sudo ./firecracker_src/tools/devtool build --release

# Rename the binary to "firecracker"
sudo cp ./firecracker_src/build/cargo_target/${ARCH}-unknown-linux-musl/release/firecracker firecracker
