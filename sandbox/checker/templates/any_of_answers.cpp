// checker template: any_of_answers
// 适用：答案不唯一，但可以从输入和标程答案推导出「正确答案必须满足的某个值/条件」。
// 例如：最大值、最小值、和、某种等价表示等。
// 使用方式：use_checker_template("any_of_answers") 安装骨架，
//          然后 read_file("checker.cpp") 查看 TODO 部分，再用 write_checker 替换判定逻辑。

#include "testlib.h"
#include <bits/stdc++.h>

using namespace std;

int main(int argc, char *argv[]) {
    registerTestlibCmd(argc, argv);

    // inf: 题目输入
    // ouf: 选手输出
    // ans: 标程答案（参考）

    // ---- TODO: 读取 inf 和 ans 的格式，推导出正确答案必须满足的条件 ----
    // 示例：
    // int n = inf.readInt(1, 100000, "n");
    // inf.readEoln();
    // ...
    // long long expected = ans.readLong();  // 或从输入计算

    // ---- TODO: 读取 ouf 的格式，并检查是否与上述条件一致 ----
    // 若一致：quitf(_ok, "accepted");
    // 否则：quitf(_wa, "...");
    // 格式错误用 quitf(_pe, "...");

    // 默认占位：未替换 TODO 时必须失败，避免不读 ouf 就 _ok 触发 dirt/PE。
    quitf(_fail, "TODO: replace template placeholder");
}
