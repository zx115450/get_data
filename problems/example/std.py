"""示例标程：读 n 和 n 个整数，输出它们的和。

输入格式：
    n
    a1 a2 ... an
输出：
    sum
"""
import sys


def main():
    data = sys.stdin.read().strip().split()
    if not data:
        print(0)
        return
    n = int(data[0])
    a = list(map(int, data[1:1 + n]))
    print(sum(a))


if __name__ == "__main__":
    main()
