use std::env;
use std::fs::{self, File};
use std::io::{BufWriter, Write};

mod ops;

fn usage() -> ! {
    eprintln!("Usage: bench -p <OP> -t <TOTAL> [-c <CONCURRENCY>] [--no-per-op-usage]");
    eprintln!();
    eprintln!("  -p  operation name");
    eprintln!("  -t  total number of operations");
    eprintln!("  -c  concurrency (default: total)");
    eprintln!("  --no-per-op-usage  disable per-op thread rusage counters");
    eprintln!();
    eprintln!("Output: results/<OP>.csv");
    eprintln!();
    eprintln!("Available ops:");
    for name in ops::list_ops() {
        eprintln!("  {}", name);
    }
    std::process::exit(1);
}

fn parse_args() -> (String, usize, usize, bool) {
    let args: Vec<String> = env::args().collect();
    let mut op = None;
    let mut total = None;
    let mut concurrency = None;
    let mut measure_per_op_usage = true;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "-p" | "--op" => {
                i += 1;
                op = Some(args[i].clone());
            }
            "-t" | "--total" => {
                i += 1;
                total = Some(args[i].parse::<usize>().unwrap());
            }
            "-c" | "--concurrency" => {
                i += 1;
                concurrency = Some(args[i].parse::<usize>().unwrap());
            }
            "--per-op-usage" => {
                measure_per_op_usage = true;
            }
            "--no-per-op-usage" => {
                measure_per_op_usage = false;
            }
            _ => usage(),
        }
        i += 1;
    }

    let op = op.unwrap_or_else(|| usage());
    let total = total.unwrap_or_else(|| usage());
    let concurrency = concurrency.unwrap_or(total);
    if total == 0 || concurrency == 0 {
        usage();
    }
    (op, total, concurrency.min(total), measure_per_op_usage)
}

fn main() {
    let (op_name, total, concurrency, measure_per_op_usage) = parse_args();
    let op = ops::get_op(&op_name).unwrap_or_else(|| {
        eprintln!("Unknown op: {}", op_name);
        usage();
    });
    let config = ops::RunConfig {
        total,
        concurrency,
        measure_per_op_usage,
    };

    let output = match op {
        ops::Op::External(op_fn) => ops::run_external(op_fn, config),
        ops::Op::ExternalWithPrepare(prepare_fn) => {
            ops::run_external_with_prepare(prepare_fn, config)
        }
        ops::Op::Internal(op_fn) => op_fn(config),
    }
    .unwrap_or_else(|err| {
        eprintln!("{} failed: {}", op_name, err);
        std::process::exit(1);
    });

    fs::create_dir_all("results").unwrap();
    let path = format!("results/{}.csv", op_name);
    let mut w = BufWriter::new(File::create(&path).unwrap());
    writeln!(
        w,
        concat!(
            "thread_id,iteration,instance_id,stage,start_us,end_us,elapsed_us,",
            "user_cpu_us,sys_cpu_us,blocked_us,minflt,majflt,nvcsw,nivcsw"
        )
    )
    .unwrap();
    for row in &output.measurements {
        writeln!(
            w,
            "{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
            row.thread_id,
            row.iteration,
            row.instance_id,
            row.stage,
            row.start_us,
            row.end_us,
            row.elapsed_us,
            row.user_cpu_us,
            row.sys_cpu_us,
            row.blocked_us,
            row.minflt,
            row.majflt,
            row.nvcsw,
            row.nivcsw
        )
        .unwrap();
    }

    println!(
        concat!(
            "run_total ",
            "op={} total={} concurrency={} per_op_usage={} ",
            "wall_us={} user_cpu_us={} sys_cpu_us={} ",
            "minflt={} majflt={} nvcsw={} nivcsw={}"
        ),
        op_name,
        total,
        concurrency,
        measure_per_op_usage,
        output.wall_us,
        output.total_usage.user_cpu_us,
        output.total_usage.sys_cpu_us,
        output.total_usage.minflt,
        output.total_usage.majflt,
        output.total_usage.nvcsw,
        output.total_usage.nivcsw
    );
}
