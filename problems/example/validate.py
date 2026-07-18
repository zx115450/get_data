"""临时手写校验器，仅用于冒烟测试 pipeline。"""
import sys


def main():
    data = sys.stdin.read().strip().split()
    if not data:
        print("empty input", file=sys.stderr)
        sys.exit(1)
    try:
        n = int(data[0])
    except ValueError:
        print(f"n not int: {data[0]}", file=sys.stderr)
        sys.exit(1)
    if not (1 <= n <= 100000):
        print(f"n out of range: {n}", file=sys.stderr)
        sys.exit(1)
    if len(data) != 1 + n:
        print(f"expected {n} numbers, got {len(data)-1}", file=sys.stderr)
        sys.exit(1)
    for x in data[1:]:
        try:
            v = int(x)
        except ValueError:
            print(f"not int: {x}", file=sys.stderr)
            sys.exit(1)
        if not (-1000000000 <= v <= 1000000000):
            print(f"ai out of range: {v}", file=sys.stderr)
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
