// checker template: tree_parent
// 适用：树相关构造题，选手输出每个节点的父节点 / 树的边集，checker 检查其是否构成合法树并满足性质。
// 使用方式：use_checker_template("tree_parent") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出（父节点数组或边集）
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 中的基础信息 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // inf.readEof();

    // ---- TODO: 读取 ouf 中的树结构 ----
    // 示例（父节点数组）：
    // vector<int> parent(n + 1);
    // for (int i = 2; i <= n; ++i) {
    //     parent[i] = ouf.readInt(1, n, "parent[i]");
    //     if (i + 1 <= n) ouf.readSpace();
    // }
    // ouf.readEoln();
    // ouf.readEof();

    // ---- TODO: 检查树结构合法性 ----
    // 1. 是否构成 n 个节点、n-1 条边、连通、无环
    // 2. 根节点是否符合题意
    // 3. 是否满足题面要求的额外性质（如深度限制、子树大小等）
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：未替换 TODO 时必须失败，避免不读 ouf 就 _ok 触发 dirt/PE。
    quitf(_fail, "TODO: replace template placeholder");
}
