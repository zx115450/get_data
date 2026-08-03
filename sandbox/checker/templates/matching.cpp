// checker template: matching
// 适用：选手输出一个匹配/配对方案，checker 检查其合法性（无公共点、边存在、权重等）。
// 使用方式：use_checker_template("matching") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出（匹配方案）
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 中的基础信息 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // int m = inf.readInt(0, 200000, "m");
    // inf.readEoln();
    // vector<pair<int,int>> edges;
    // for (int i = 0; i < m; ++i) {
    //     int u = inf.readInt(1, n, "u");
    //     int v = inf.readInt(1, n, "v");
    //     edges.push_back({u, v});
    //     inf.readEoln();
    // }
    // inf.readEof();

    // ---- TODO: 读取 ouf 中的匹配 ----
    // 示例：
    // int k = ouf.readInt(0, n / 2, "k");
    // ouf.readEoln();
    // vector<pair<int,int>> matched(k);
    // for (int i = 0; i < k; ++i) {
    //     int u = ouf.readInt(1, n, "u");
    //     ouf.readSpace();
    //     int v = ouf.readInt(1, n, "v");
    //     matched[i] = {u, v};
    //     ouf.readEoln();
    // }
    // ouf.readEof();

    // ---- TODO: 检查匹配合法性 ----
    // 1. 每条边是否存在于图中（或题面允许的边集合）
    // 2. 各点是否至多出现一次
    // 3. 大小/权重是否满足题意
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：未替换 TODO 时必须失败，避免不读 ouf 就 _ok 触发 dirt/PE。
    quitf(_fail, "TODO: replace template placeholder");
}
