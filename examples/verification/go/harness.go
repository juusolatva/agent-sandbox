package main

import (
	"bufio"
	"fmt"
	"os"

	smoke "sandbox.example/smoke"
)

func main() {
	input := bufio.NewScanner(os.Stdin)
	for input.Scan() {
		var left, right int
		if _, err := fmt.Sscan(input.Text(), &left, &right); err != nil {
			panic(err)
		}
		fmt.Printf("answer: %d\n", smoke.Add(left, right))
	}
}
