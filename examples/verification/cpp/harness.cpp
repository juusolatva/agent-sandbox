#include "calculator.h"
#include <iostream>

int main() {
    int left, right;
    while (std::cin >> left >> right) std::cout << "answer: " << add(left, right) << '\n';
}
