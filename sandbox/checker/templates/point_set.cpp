// checker template: point_set
// 适用：几何构造题，选手输出一个点集 / 多边形 / 坐标列表，checker 检查其合法性。
// 使用方式：use_checker_template("point_set") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出（点集或多边形）
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 中的基础信息 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // inf.readEof();

    // ---- TODO: 读取 ouf 中的点集 ----
    // 示例：
    // int k = ouf.readInt(1, n, "k");
    // ouf.readEoln();
    // vector<pair<int,int>> pts(k);
    // for (int i = 0; i < k; ++i) {
    //     int x = ouf.readInt(-1e9, 1e9, "x");
    //     ouf.readSpace();
    //     int y = ouf.readInt(-1e9, 1e9, "y");
    //     pts[i] = {x, y};
    //     ouf.readEoln();
    // }
    // ouf.readEof();

    // ---- TODO: 检查点集合法性 ----
    // 1. 坐标范围、点数是否符合题意
    // 2. 是否满足几何性质：不重合、凸包、简单多边形、包含/覆盖关系等
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：直接认为通过。模型必须替换上述 TODO 逻辑。
    quitf(_ok, "accepted by template placeholder");
}
