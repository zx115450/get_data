// checker template: subset
// 适用：选手需要输出一个子集/选择方案，checker 检查其元素合法性与题意约束。
// 使用方式：use_checker_template("subset") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出（子集/选择的元素）
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 中的基础信息 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // vector<long long> a(n);
    // for (int i = 0; i < n; ++i) {
    //     a[i] = inf.readLong();
    //     if (i + 1 < n) inf.readSpace();
    // }
    // inf.readEoln();
    // inf.readEof();

    // ---- TODO: 读取 ouf 中的子集 ----
    // 示例：
    // int k = ouf.readInt(0, n, "k");
    // ouf.readEoln();
    // vector<int> chosen(k);
    // for (int i = 0; i < k; ++i) {
    //     chosen[i] = ouf.readInt(1, n, "index");
    //     if (i + 1 < k) ouf.readSpace();
    // }
    // ouf.readEoln();
    // ouf.readEof();

    // ---- TODO: 检查子集合法性 ----
    // 1. 下标是否合法、是否重复（若题面要求不重复）
    // 2. 按题意检查约束（如和等于目标、大小符合要求、某种覆盖性质等）
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：未替换 TODO 时必须失败，避免不读 ouf 就 _ok 触发 dirt/PE。
    quitf(_fail, "TODO: replace template placeholder");
}
