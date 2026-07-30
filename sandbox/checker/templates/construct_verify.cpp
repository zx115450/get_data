// checker template: construct_verify
// 适用：选手需要输出一个构造/方案，checker 负责检查其格式合法性与题意正确性。
// 使用方式：用 use_checker_template("construct_verify") 安装骨架，
//          然后 read_file("checker.cpp") 查看，再用 write_checker 替换 TODO 部分。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出
    // ans: 标程答案（仅作参考，不要直接字符串比对，除非题面明确要求）

    // ---- TODO: 按题面读取 inf 和 ouf 的格式 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // ...

    // ---- TODO: 检查 ouf 的格式合法性 ----
    // 用 ouf.readInt / readToken / readEoln / readEof 等严格读取。
    // 任何格式不符用 quitf(_pe, "...") 报告。

    // ---- TODO: 检查 ouf 是否满足题意 ----
    // 例如：构造是否合法、代价是否最小、路径是否有效等。
    // 若满足：quitf(_ok, "accepted");
    // 否则：quitf(_wa, "...");

    // 默认占位：直接认为通过。模型必须替换上述 TODO 逻辑。
    quitf(_ok, "accepted by template placeholder");
}
