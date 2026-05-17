use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

/// Hash 1KB of data 1000 times. Pure CPU work.
pub fn run() -> Result<(), String> {
    let data = [0xABu8; 1024];
    let mut h = DefaultHasher::new();
    for _ in 0..1000 {
        data.hash(&mut h);
    }
    // Prevent optimization
    std::hint::black_box(h.finish());
    Ok(())
}
