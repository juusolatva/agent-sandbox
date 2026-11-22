use std::io::BufRead;

fn main() {
    for line in std::io::stdin().lock().lines() {
        let line = line.expect("read case");
        let mut values = line.split_whitespace().map(|v| v.parse::<i32>().expect("integer case"));
        let (left, right) = (values.next().expect("left"), values.next().expect("right"));
        println!("answer: {}", sandbox_smoke::add(left, right));
    }
}
