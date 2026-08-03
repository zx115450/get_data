// checker template: permutation
// 适用：选手输出一个 1..n 的排列，checker 检查其合法性和题意相关性质。
// 使用方式：use_checker_template("permutation") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出（排列）
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 中的 n 及相关信息 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // inf.readEof();

    // ---- TODO: 读取 ouf 的排列 ----
    // 示例：
    // vector<int> p(n);
    // for (int i = 0; i < n; ++i) {
    //     p[i] = ouf.readInt(1, n, "p[i]");
    //     if (i + 1 < n) ouf.readSpace();
    // }
    // ouf.readEoln();
    // ouf.readEof();

    // ---- TODO: 检查排列合法性 ----
    // 1. 是否恰好 1..n 各出现一次
    // 2. 按题意检查排列是否满足所需性质（如某种顺序、逆序对限制、与输入数组的关系等）
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：未替换 TODO 时必须失败，避免不读 ouf 就 _ok 触发 dirt/PE。
    quitf(_fail, "TODO: replace template placeholder");
}
