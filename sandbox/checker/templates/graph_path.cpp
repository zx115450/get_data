// checker template: graph_path
// 适用：选手需要输出一条路径/环/ walk，checker 负责检查其合法性。
// 验证项通常包括：起点终点、相邻点有边、不重复访问、长度/权重约束等。
// 使用方式：use_checker_template("graph_path") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入（含图结构）
    // ouf: 选手输出（路径）
    // ans: 标程答案（参考）

    // ---- TODO: 从 inf 读取图 ----
    // 示例：
    // int n = inf.readInt(2, 100000, "n");
    // int m = inf.readInt(1, 200000, "m");
    // inf.readEoln();
    // vector<unordered_set<int>> adj(n + 1);
    // for (int i = 0; i < m; ++i) {
    //     int u = inf.readInt(1, n, "u");
    //     int v = inf.readInt(1, n, "v");
    //     adj[u].insert(v);
    //     adj[v].insert(u);  // 无向图；有向图只插单向
    //     inf.readEoln();
    // }
    // inf.readEof();

    // ---- TODO: 从 ouf 读取路径 ----
    // 示例：
    // int k = ouf.readInt(1, n, "k");
    // ouf.readEoln();
    // vector<int> path(k);
    // for (int i = 0; i < k; ++i) {
    //     path[i] = ouf.readInt(1, n, "v");
    //     if (i + 1 < k) ouf.readSpace();
    // }
    // ouf.readEoln();
    // ouf.readEof();

    // ---- TODO: 检查路径合法性 ----
    // 1. 起点/终点是否与题意一致
    // 2. 相邻点之间是否有边
    // 3. 是否重复访问（若题面要求简单路径）
    // 4. 长度/权重是否满足约束
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：直接认为通过。模型必须替换上述 TODO 逻辑。
    quitf(_ok, "accepted by template placeholder");
}
