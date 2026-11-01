package main
import smoke "sandbox.example/smoke"
func main() {
    if smoke.Add(2, 2) != 4 || smoke.Add(-1, 1) != 0 { panic("protected check failed") }
    println("Protected Go checks passed")
}
