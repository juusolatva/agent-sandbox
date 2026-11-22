#include "calculator.h"
#include <iostream>

int main() {
    int left = 0;
    int right = 0;
    while (std::cin >> left >> right) std::cout << "answer: " << add(left, right) << '\n';
}
