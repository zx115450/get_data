"""临时手写生成器，仅用于冒烟测试 pipeline（非 Agent 产物）。"""
import argparse
import random
import sys


def gen(n, a_min, a_max, typ):
    if typ == "edge_n1":
        vals = [random.randint(a_min, a_max)]
    elif typ == "edge_nmax":
        vals = [random.randint(a_min, a_max) for _ in range(n)]
    elif typ == "all_equal":
        v = random.randint(a_min, a_max)
        vals = [v] * n
    elif typ == "descending":
        vals = sorted([random.randint(a_min, a_max) for _ in range(n)], reverse=True)
    else:  # random
        vals = [random.randint(a_min, a_max) for _ in range(n)]
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--type", default="random")
    args = ap.parse_args()

    random.seed(args.seed)
    n_min, n_max = 1, 100000
    a_min, a_max = -1000000000, 1000000000

    if args.type == "edge_n1":
        n = 1
    elif args.type == "edge_nmax":
        n = n_max
    else:
        n = random.randint(n_min, min(100, n_max))  # 测试用，缩小规模

    vals = gen(n, a_min, a_max, args.type)
    print(n)
    print(" ".join(map(str, vals)))


if __name__ == "__main__":
    main()
