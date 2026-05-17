// Copyright 2026
// SPDX-License-Identifier: Apache-2.0

//! UFFD handler that serves Firecracker snapshot memory pages from a remote
//! HTTP blob using Range requests.

mod uffd_utils;

use std::collections::HashMap;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::os::unix::net::UnixListener;

use uffd_utils::{Runtime, UffdHandler, install_shutdown_signal_handlers};

const FETCH_CHUNK_SIZE: usize = 4 * 1024;

struct HttpUrl {
    host: String,
    port: u16,
    path: String,
    host_header: String,
}

impl HttpUrl {
    fn parse(url: &str) -> Self {
        let rest = url
            .strip_prefix("http://")
            .unwrap_or_else(|| panic!("only http:// URLs are supported: {url}"));
        let (authority, path) = match rest.split_once('/') {
            Some((authority, path)) => (authority, format!("/{path}")),
            None => (rest, "/".to_string()),
        };
        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) => (host.to_string(), port.parse().expect("invalid URL port")),
            None => (authority.to_string(), 80),
        };

        Self {
            host,
            port,
            path,
            host_header: authority.to_string(),
        }
    }
}

struct HttpRangeClient {
    url: HttpUrl,
    reader: Option<BufReader<TcpStream>>,
}

impl HttpRangeClient {
    fn new(url: &str) -> Self {
        Self {
            url: HttpUrl::parse(url),
            reader: None,
        }
    }

    fn connect(&self) -> io::Result<BufReader<TcpStream>> {
        let stream = TcpStream::connect((self.url.host.as_str(), self.url.port))?;
        stream.set_nodelay(true)?;
        Ok(BufReader::new(stream))
    }

    fn fetch_range(&mut self, offset: u64, len: usize) -> io::Result<Vec<u8>> {
        match self.fetch_range_once(offset, len) {
            Ok(data) => Ok(data),
            Err(_) => {
                self.reader = None;
                self.fetch_range_once(offset, len)
            }
        }
    }

    fn fetch_range_once(&mut self, offset: u64, len: usize) -> io::Result<Vec<u8>> {
        if self.reader.is_none() {
            self.reader = Some(self.connect()?);
        }

        let reader = self.reader.as_mut().unwrap();
        let end = offset + len as u64 - 1;
        write!(
            reader.get_mut(),
            "GET {} HTTP/1.1\r\nHost: {}\r\nRange: bytes={}-{}\r\nConnection: keep-alive\r\n\r\n",
            self.url.path,
            self.url.host_header,
            offset,
            end
        )?;
        reader.get_mut().flush()?;

        let mut status_line = String::new();
        reader.read_line(&mut status_line)?;
        let status = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|value| value.parse::<u16>().ok())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad HTTP status line"))?;

        let mut content_len = None;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line)?;
            if line == "\r\n" || line == "\n" {
                break;
            }
            let lower = line.to_ascii_lowercase();
            if let Some(value) = lower.strip_prefix("content-length:") {
                content_len = Some(value.trim().parse::<usize>().map_err(|_| {
                    io::Error::new(io::ErrorKind::InvalidData, "bad Content-Length")
                })?);
            }
        }

        if status != 206 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("expected HTTP 206 for Range request, got {status}"),
            ));
        }
        if content_len != Some(len) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("expected {len} response bytes, got {content_len:?}"),
            ));
        }

        let mut data = vec![0u8; len];
        reader.read_exact(&mut data)?;
        Ok(data)
    }
}

fn serve_remote_pf(
    uffd_handler: &mut UffdHandler,
    client: &mut HttpRangeClient,
    cache: &mut HashMap<u64, Vec<u8>>,
    addr: *mut u8,
) -> bool {
    let page_size = uffd_handler.page_size;
    let (dst, file_offset) = uffd_handler.file_offset_for_addr(addr);
    let remote_size = uffd_handler
        .mem_regions
        .iter()
        .map(|region| region.offset + region.size as u64)
        .max()
        .expect("missing UFFD memory regions");
    let chunk_base = file_offset & !((FETCH_CHUNK_SIZE as u64) - 1);
    let chunk_len = usize::try_from((remote_size - chunk_base).min(FETCH_CHUNK_SIZE as u64))
        .expect("invalid remote chunk length");
    let page_offset = usize::try_from(file_offset - chunk_base).expect("invalid page offset");

    if !cache.contains_key(&chunk_base) {
        let data = client
            .fetch_range(chunk_base, chunk_len)
            .expect("remote range fetch failed");
        cache.insert(chunk_base, data);
    }

    let chunk = cache.get(&chunk_base).expect("missing cached chunk");
    uffd_handler.copy_from_slice(dst, &chunk[page_offset..page_offset + page_size])
}

fn main() {
    let mut args = std::env::args();
    let uffd_sock_path = args.nth(1).expect("No socket path given");
    let memory_blob_url = args.next().expect("No memory blob URL given");
    let _sandbox_id = args.next().unwrap_or_else(|| "0".to_string());

    let listener = UnixListener::bind(uffd_sock_path).expect("Cannot bind to socket path");
    let (stream, _) = listener.accept().expect("Cannot listen on UDS socket");

    let mut client = HttpRangeClient::new(&memory_blob_url);
    let mut cache = HashMap::new();
    let mut runtime = Runtime::new_remote(stream);
    runtime.install_panic_hook();
    install_shutdown_signal_handlers();
    let mut copied_pages = 0u64;
    runtime.run(|uffd_handler: &mut UffdHandler| {
        let mut deferred_events = Vec::new();

        loop {
            let mut events_to_handle = Vec::from_iter(deferred_events.drain(..));

            while let Some(event) = uffd_handler.read_event().expect("Failed to read uffd_msg") {
                events_to_handle.push(event);
            }

            for event in events_to_handle.drain(..) {
                match event {
                    userfaultfd::Event::Pagefault { addr, .. } => {
                        if serve_remote_pf(
                            uffd_handler,
                            &mut client,
                            &mut cache,
                            addr.cast(),
                        ) {
                            copied_pages += 1;
                        } else {
                            deferred_events.push(event);
                        }
                    }
                    userfaultfd::Event::Remove { start, end } => {
                        uffd_handler.unregister_range(start, end)
                    }
                    _ => panic!("Unexpected event on userfaultfd"),
                }
            }

            if deferred_events.is_empty() {
                break;
            }
        }
    });
    println!("COPIED_PAGES={copied_pages}");
}
