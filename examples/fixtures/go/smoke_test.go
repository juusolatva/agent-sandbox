package smoke

import "testing"

func TestSmoke(t *testing.T) {
	if 2+2 != 4 { t.Fatal("arithmetic failed") }
}
