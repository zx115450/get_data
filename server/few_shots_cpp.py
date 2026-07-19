
"""C++ testlib 版 few-shot 样板库。由 shots/*.txt 装配生成。"""

CPP_ARRAY_EXAMPLE = r"""
【参考范例：一道数组题的标准写法（C++ testlib）】
题面：给定 n 和 n 个整数 a1..an，输出它们的和。
输入格式：第 1 行 n；第 2 行 n 个整数空格分隔。
输出格式：一个整数。
数据范围：n in [1,1e5]，ai in [-1e9,1e9]，默认 15 组，覆盖 n=1、n=max、全相等、降序；其余组按 index 在 [1,1e5] 上均匀分层（既有小也有大）。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 100000], "ai": [-1000000000, 1000000000]},
  "edge_cases": ["edge_n1", "edge_nmax", "all_equal", "descending"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
// 把 [lo,hi] 按 --index/--count 切成 cnt 段，第 idx 组落在第 idx 段内，保证规模均匀
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0;
    if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt;
    long long b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a;
    if (a > hi) a = hi;
    if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int seed = opt<int>("seed");
    string typ = opt<string>("type", "random");
    int N_MIN = 1, N_MAX = 100000;
    long long A_MIN = -1000000000LL, A_MAX = 1000000000LL;
    int n;
    if (typ == "edge_n1") n = 1;
    else if (typ == "edge_nmax") n = N_MAX;
    else n = pickSized(N_MIN, N_MAX);
    vector<long long> a(n);
    if (typ == "all_equal") {
        long long v = rnd.next(A_MIN, A_MAX);
        for (int i = 0; i < n; i++) a[i] = v;
    } else if (typ == "descending") {
        for (int i = 0; i < n; i++) a[i] = rnd.next(A_MIN, A_MAX);
        sort(a.begin(), a.end(), greater<long long>());
    } else {
        for (int i = 0; i < n; i++) a[i] = rnd.next(A_MIN, A_MAX);
    }
    printf("%d\n", n);
    for (int i = 0; i < n; i++) printf("%lld%c", a[i], i + 1 < n ? ' ' : '\n');
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 100000, "n");
    inf.readEoln();
    for (int i = 0; i < n; i++) {
        inf.readLong(-1000000000LL, 1000000000LL, "ai");
        if (i + 1 < n) inf.readSpace();
    }
    inf.readEoln();
    inf.readEof();
    return 0;
}

"""

CPP_TREE_EXAMPLE = r"""
【参考范例：一道树题的标准写法（C++ testlib）】
题面：给定一棵 n 个节点的无权无根树，求树的直径（最长简单路径的边数）。
输入格式：第 1 行 n；接下来 n-1 行每行两个整数 u v 表示一条边（节点编号 1..n）。
输出格式：一个整数。
数据范围：n in [2,1e5]，默认 15 组，覆盖链、菊花、随机树、平衡二叉树、n=2；其余组 n 按 index 在 [2,1e5] 分层。

range.json:
{
  "count": 15,
  "constraints": {"n": [2, 100000]},
  "edge_cases": ["chain", "star", "random_tree", "balanced_binary", "edge_n2"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0;
    if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt;
    long long b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a;
    if (a > hi) a = hi;
    if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
vector<pair<int,int>> build(int n, string typ) {
    vector<pair<int,int>> e;
    if (n == 1) return e;
    if (typ == "chain") { for (int i = 1; i < n; i++) e.push_back({i, i + 1}); return e; }
    if (typ == "star") { for (int i = 2; i <= n; i++) e.push_back({1, i}); return e; }
    if (typ == "balanced_binary") { for (int i = 2; i <= n; i++) e.push_back({i / 2, i}); return e; }
    for (int i = 2; i <= n; i++) e.push_back({rnd.next(1, i - 1), i});
    return e;
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int seed = opt<int>("seed");
    string typ = opt<string>("type", "random_tree");
    int N_MIN = 2, N_MAX = 100000;
    int n;
    if (typ == "edge_n2") n = 2;
    else n = pickSized(N_MIN, N_MAX);
    auto e = build(n, typ);
    printf("%d\n", n);
    for (auto p : e) printf("%d %d\n", p.first, p.second);
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
const int MAXN = 100005;
int par[MAXN];
int find(int x) { return par[x] == x ? x : par[x] = find(par[x]); }
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(2, 100000, "n");
    inf.readEoln();
    for (int i = 1; i <= n; i++) par[i] = i;
    set<pair<int,int>> es;
    for (int i = 0; i < n - 1; i++) {
        int u = inf.readInt(1, n, "u"); inf.readSpace();
        int v = inf.readInt(1, n, "v"); inf.readEoln();
        ensuref(u != v, "self loop at %d", u);
        auto key = make_pair(min(u, v), max(u, v));
        ensuref(es.insert(key).second, "duplicate edge %d %d", u, v);
        int ru = find(u), rv = find(v);
        ensuref(ru != rv, "cycle at %d %d", u, v);
        par[ru] = rv;
    }
    int root = find(1);
    for (int i = 2; i <= n; i++) ensuref(find(i) == root, "not connected");
    inf.readEof();
    return 0;
}

"""

CPP_GRAPH_EXAMPLE = r"""
【参考范例：一道图题的标准写法（C++ testlib）】
题面：给定 n 个点 m 条边的无向图（无自环无重边），判断是否连通。
说明：本例默认无自环无重边。若目标题允许自环/重边，请去掉 gen 中 u<v 的池子限制以及 validator 中 u!=v 和重复边检查，按题面为准。
注意：random_sparse 分支用 unordered_set 随机采样，是「从 N 个候选选 K 个不重复」的标准写法；严禁枚举所有 n*(n-1)/2 条边再 shuffle（n=2e5 时必爆）。
输入格式：第 1 行 n m；接下来 m 行每行 u v。
输出格式：YES 或 NO。
数据范围：n in [1,1000]，m in [0,n*(n-1)/2]，15 组，覆盖连通树、不连通、完全图、链、菊花、随机稀疏。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 1000], "m": [0, 499500]},
  "edge_cases": ["connected_tree", "disconnected", "complete", "path", "star", "random_sparse"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
// 把 [lo,hi] 按 --index/--count 切成 cnt 段，第 idx 组落在第 idx 段内，保证规模均匀
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0;
    if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt;
    long long b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a;
    if (a > hi) a = hi;
    if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int seed = opt<int>("seed");
    string typ = opt<string>("type", "random_sparse");
    int N_MIN = 1, N_MAX = 1000;
    int n = pickSized(N_MIN, N_MAX);
    vector<pair<int,int>> edges;
    if (typ == "connected_tree") { for (int i = 2; i <= n; i++) edges.push_back({rnd.next(1, i - 1), i}); }
    else if (typ == "disconnected") {
        int mid = n / 2 ? n / 2 : 1;
        for (int i = 2; i <= mid; i++) edges.push_back({rnd.next(1, i - 1), i});
        for (int i = mid + 2; i <= n; i++) edges.push_back({rnd.next(mid + 1, i - 1), i});
    } else if (typ == "complete") { for (int u = 1; u <= n; u++) for (int v = u + 1; v <= n; v++) edges.push_back({u, v}); }
    else if (typ == "path") { for (int i = 1; i < n; i++) edges.push_back({i, i + 1}); }
    else if (typ == "star") { for (int i = 2; i <= n; i++) edges.push_back({1, i}); }
    else {
        // random_sparse: 用 unordered_set 随机采样 m 条不重复边，O(m) 而非 O(n^2)。
        // 关键写法：n 很大时不要枚举所有 n*(n-1)/2 条边再 shuffle，会爆内存/时间。
        int m = rnd.next(0, max(1, n));
        unordered_set<long long> used;
        used.reserve(m * 2);
        while ((int)edges.size() < m) {
            int u = rnd.next(1, n);
            int v = rnd.next(1, n);
            if (u == v) continue;
            long long key = (long long)min(u, v) * (n + 1) + max(u, v);
            if (!used.insert(key).second) continue;
            edges.push_back({u, v});
        }
    }
    printf("%d %d\n", n, (int)edges.size());
    for (auto e : edges) printf("%d %d\n", e.first, e.second);
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 1000, "n"); inf.readSpace();
    int m = inf.readInt(0, 499500, "m"); inf.readEoln();
    set<pair<int,int>> es;
    for (int i = 0; i < m; i++) {
        int u = inf.readInt(1, n, "u"); inf.readSpace();
        int v = inf.readInt(1, n, "v"); inf.readEoln();
        ensuref(u != v, "self loop %d", u);
        ensuref(es.insert(make_pair(min(u, v), max(u, v))).second, "duplicate edge %d %d", u, v);
    }
    inf.readEof();
    return 0;
}

"""

CPP_STRING_EXAMPLE = r"""
【参考范例：一道字符串题的标准写法（C++ testlib）】
题面：给定长度为 n 的小写字母字符串 s 和模式串 p，输出 p 在 s 中作为子串出现的次数。
输入格式：第 1 行 n；第 2 行 s；第 3 行 p。
输出格式：一个整数。
数据范围：n in [1,1000]，p 长度 in [1,n]，15 组，覆盖全相同、模式在首/尾、无匹配、长连续段。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 1000]},
  "edge_cases": ["all_same", "pattern_at_start", "pattern_at_end", "no_match", "long_run"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
// 把 [lo,hi] 按 --index/--count 切成 cnt 段，第 idx 组落在第 idx 段内，保证规模均匀
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0;
    if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt;
    long long b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a;
    if (a > hi) a = hi;
    if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int seed = opt<int>("seed");
    string typ = opt<string>("type", "random");
    int N_MIN = 1, N_MAX = 1000;
    int n = pickSized(N_MIN, N_MAX);
    string s, p;
    if (typ == "all_same" || typ == "long_run") {
        char c = 'a' + rnd.next(0, 25);
        s = string(n, c);
    } else {
        s = "";
        for (int i = 0; i < n; i++) s += char('a' + rnd.next(0, 25));
    }
    int plen = rnd.next(1, max(1, n));
    if (typ == "pattern_at_start") p = s.substr(0, plen);
    else if (typ == "pattern_at_end") p = s.substr(n - plen, plen);
    else if (typ == "no_match") {
        do { p = ""; for (int i = 0; i < plen; i++) p += char('a' + rnd.next(0, 25)); } while (s.find(p) != string::npos);
    } else p = s.substr(0, plen);
    printf("%d\n", n);
    printf("%s\n", s.c_str());
    printf("%s\n", p.c_str());
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 1000, "n"); inf.readEoln();
    string s = inf.readToken("[a-z]+", "s"); inf.readEoln();
    string p = inf.readToken("[a-z]+", "p"); inf.readEoln();
    ensuref((int)s.size() == n, "|s|=%d != n=%d", (int)s.size(), n);
    ensuref((int)p.size() >= 1 && (int)p.size() <= n, "p len out of range");
    inf.readEof();
    return 0;
}

"""

CPP_NUMBER_THEORY_EXAMPLE = r"""
【参考范例：一道数论题的标准写法（C++ testlib）】
题面：给定 n 个正整数，输出它们的最大公约数。
输入格式：第 1 行 n；第 2 行 n 个正整数空格分隔。
输出格式：一个整数。
数据范围：n in [1,1e5]，ai in [1,1e9]，15 组，覆盖 n=1、全相等、全素数、含两两互素、全偶。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 100000], "ai": [1, 1000000000]},
  "edge_cases": ["edge_n1", "all_equal", "all_prime", "coprime_pair", "all_even"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
bool isPrime(long long x) { if (x < 2) return false; for (long long i = 2; i * i <= x; i++) if (x % i == 0) return false; return true; }
// 把 [lo,hi] 按 --index/--count 切成 cnt 段，第 idx 组落在第 idx 段内，保证规模均匀
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0;
    if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt;
    long long b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a;
    if (a > hi) a = hi;
    if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int seed = opt<int>("seed");
    string typ = opt<string>("type", "random");
    int N_MIN = 1, N_MAX = 100000;
    long long A_MIN = 1, A_MAX = 1000000000LL;
    int n;
    if (typ == "edge_n1") n = 1;
    else n = pickSized(N_MIN, N_MAX);
    vector<long long> a(n);
    if (typ == "all_equal") { long long v = rnd.next(A_MIN, A_MAX); for (int i = 0; i < n; i++) a[i] = v; }
    else if (typ == "all_prime") { vector<long long> pr; for (long long x = 2; x < 200; x++) if (isPrime(x)) pr.push_back(x); for (int i = 0; i < n; i++) a[i] = pr[rnd.next(0, (int)pr.size() - 1)]; }
    else if (typ == "coprime_pair") { vector<long long> pr; for (long long x = 2; x < 500; x++) if (isPrime(x)) pr.push_back(x); for (int i = (int)pr.size() - 1; i > 0; i--) { int j = rnd.next(0, i); swap(pr[i], pr[j]); } for (int i = 0; i < n; i++) a[i] = pr[i]; }
    else if (typ == "all_even") { for (int i = 0; i < n; i++) a[i] = rnd.next(A_MIN, A_MAX / 2) * 2; }
    else { for (int i = 0; i < n; i++) a[i] = rnd.next(A_MIN, A_MAX); }
    printf("%d\n", n);
    for (int i = 0; i < n; i++) printf("%lld%c", a[i], i + 1 < n ? ' ' : '\n');
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 100000, "n"); inf.readEoln();
    for (int i = 0; i < n; i++) {
        inf.readLong(1, 1000000000LL, "ai");
        if (i + 1 < n) inf.readSpace();
    }
    inf.readEoln();
    inf.readEof();
    return 0;
}

"""

CPP_MULTI_TEST_EXAMPLE = r"""
【参考范例：一道多测数组题的标准写法（C++ testlib）】
题面：输入第一行是测试组数 T，接下来 T 组，每组第一行 n，第二行 n 个整数，输出这 n 个数的和。保证 sum n ≤ 1e5。
输入格式：第 1 行 T；随后 T 组，每组一行 n，一行 n 个整数。
输出格式：T 行，每行一个整数。
数据范围：T∈[1,1e3]，n∈[1,1e5]，sum n≤1e5，ai∈[-1e9,1e9]，15 组，覆盖 T=1、T=max、n=1、n=max、sum 刚好压满、均衡分布。

range.json:
{
  "count": 15,
  "constraints": {"T": [1, 1000], "n": [1, 100000], "sum_n": [1, 100000], "ai": [-1000000000, 1000000000]},
  "edge_cases": ["edge_T1", "edge_Tmax", "edge_n1", "edge_nmax", "sum_full"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
// 把 [lo,hi] 按 --index/--count 切成 cnt 段，第 idx 组落在第 idx 段内，保证规模均匀
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0;
    if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt;
    long long b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a;
    if (a > hi) a = hi;
    if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random");
    int idx = opt<int>("index", 0);
    int cnt = opt<int>("count", 15);
    int T_MIN = 1, T_MAX = 1000;
    int N_MIN = 1, N_MAX = 100000;
    long long SUM = 100000;
    long long A_MIN = -1000000000LL, A_MAX = 1000000000LL;

    int T;
    bool force_small_n = false;   // 强制每个 n 都很小（攻 T 或 edge_n1）
    bool force_large_n = false;   // 强制单个 n 尽量大（攻 n 或 edge_nmax）
    bool force_sum_full = false;  // 让 sum 尽量接近上限

    if (typ == "edge_T1") T = 1;
    else if (typ == "edge_Tmax") T = T_MAX;
    else if (typ == "edge_n1") { T = 1; force_small_n = true; }
    else if (typ == "edge_nmax") { T = 1; force_large_n = true; }
    else if (typ == "sum_full") { T = pickSized(max(1, T_MAX / 4), max(1, T_MAX / 2)); force_sum_full = true; }
    else {
        // 只有 random 按 index 分桶：0=攻 T，1=均衡，2=攻 n
        T = pickSized(T_MIN, T_MAX);
        int bucket = min(2, idx * 3 / max(1, cnt));
        if (bucket == 0) { T = max(T, (int)(T_MAX * 2 / 3)); force_small_n = true; }  // 大 T 小 n
        if (bucket == 2) { T = min(T, 3); force_large_n = true; }                      // 小 T 大 n
    }

    vector<int> ns;
    long long left = SUM;
    for (int i = 0; i < T; i++) {
        int max_ni;
        if (force_small_n) max_ni = 1;
        else if (force_large_n) max_ni = min(N_MAX, (int)left);
        else if (force_sum_full) {
            max_ni = min(N_MAX, (int)(left / max(1, T - i)));
            if (i + 1 == T) max_ni = max(N_MIN, (int)left); // 最后一组把剩余压满
        }
        else max_ni = min(N_MAX, (int)(left / max(1, T - i))); // 均衡
        int ni = rnd.next(N_MIN, max(N_MIN, max_ni));
        if (ni > left) ni = max(1, (int)left);
        ns.push_back(ni);
        left -= ni;
        if (left <= 0) { T = i + 1; break; }
    }
    while ((int)ns.size() < T) { ns.push_back(1); left--; }

    printf("%d\n", T);
    for (int n : ns) {
        printf("%d\n", n);
        for (int i = 0; i < n; i++) {
            long long v = rnd.next(A_MIN, A_MAX);
            printf("%lld%c", v, i + 1 < n ? ' ' : '\n');
        }
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int T = inf.readInt(1, 1000, "T"); inf.readEoln();
    long long sum = 0;
    for (int t = 0; t < T; t++) {
        int n = inf.readInt(1, 100000, "n"); inf.readEoln();
        sum += n;
        ensuref(sum <= 100000, "sum n = %lld exceeds 100000", sum);
        for (int i = 0; i < n; i++) {
            inf.readLong(-1000000000LL, 1000000000LL, "ai");
            if (i + 1 < n) inf.readSpace();
        }
        inf.readEoln();
    }
    inf.readEof();
    return 0;
}

"""

CPP_FEW_SHOTS = {
    "array": CPP_ARRAY_EXAMPLE,
    "tree": CPP_TREE_EXAMPLE,
    "graph": CPP_GRAPH_EXAMPLE,
    "string": CPP_STRING_EXAMPLE,
    "number_theory": CPP_NUMBER_THEORY_EXAMPLE,
    "multi_test": CPP_MULTI_TEST_EXAMPLE,
}
