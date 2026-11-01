fn main() {
    assert_eq!(sandbox_smoke::add(2, 2), 4);
    assert_eq!(sandbox_smoke::add(-1, 1), 0);
    println!("Protected Rust checks passed");
}
