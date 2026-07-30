// checker template: sequence_property
// 适用：选手输出一个序列/数组，checker 检查长度、元素范围及题意性质。
// 使用方式：use_checker_template("sequence_property") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出（序列）
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 中的基础信息 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // inf.readEof();

    // ---- TODO: 读取 ouf 中的序列 ----
    // 示例：
    // vector<long long> b(n);
    // for (int i = 0; i < n; ++i) {
    //     b[i] = ouf.readLong();
    //     if (i + 1 < n) ouf.readSpace();
    // }
    // ouf.readEoln();
    // ouf.readEof();

    // ---- TODO: 检查序列合法性 ----
    // 1. 长度是否等于 n
    // 2. 元素范围是否符合题意
    // 3. 题意要求的性质：单调性、相邻差、与输入序列的关系、某种不变量等
    // 满足则 quitf(_ok, ...)，否则 quitf(_wa, ...)
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：直接认为通过。模型必须替换上述 TODO 逻辑。
    quitf(_ok, "accepted by template placeholder");
}
