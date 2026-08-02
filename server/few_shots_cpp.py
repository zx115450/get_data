
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
【参考范例：一道树题的标准写法（C++ + ACM-generator）】
题面：给定一棵 n 个节点的无权无根树，求树的直径（最长简单路径的边数）。
输入格式：第 1 行 n；接下来 n-1 行每行两个整数 u v 表示一条边（节点编号 1..n）。
输出格式：一个整数。
数据范围：n in [2,1e5]，默认 15 组，覆盖链、菊花、随机树、平衡二叉树、n=2；其余组 n 按 index 在 [2,1e5] 分层。
说明：树结构优先用 generator.h（Chain/Flower/Tree）：必须先 .gen()，再 cout << tree；
自定义输出用 tree.edges()（不是 get_edges）；严禁 .shuffle()。
仍须 registerGen 与 --seed/--type/--index/--count。

range.json:
{
  "count": 15,
  "constraints": {"n": [2, 100000]},
  "edge_cases": ["chain", "star", "random_tree", "balanced_binary", "edge_n2"]
}

gen.cpp:
#include "generator.h"
using namespace std;
using namespace generator::all;
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
    string typ = opt<string>("type", "random_tree");
    int N_MIN = 2, N_MAX = 100000;
    int n = (typ == "edge_n2") ? 2 : pickSized(N_MIN, N_MAX);
    if (typ == "chain") {
        unweight::Chain tree(n);
        tree.gen();
        cout << tree << "\n";
    } else if (typ == "star") {
        unweight::Flower tree(n);
        tree.gen();
        cout << tree << "\n";
    } else if (typ == "balanced_binary") {
        printf("%d\n", n);
        for (int i = 2; i <= n; i++) printf("%d %d\n", i / 2, i);
    } else {
        unweight::Tree tree(n);
        tree.gen();
        cout << tree << "\n";
    }
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
【参考范例：一道图题的标准写法（C++ + ACM-generator）】
【仅供参考】下面范例是「单测、无向、无自环无重边、判连通」的示意。写本题 gen 时必须以【本题题面 / 标程 / range.json / gen_plan】为准：
- 若标程先读 T：stdout 第一行必须是 T∈[Tmin,Tmax]，禁止照抄本范例「第一行 n m」。
- 若题面允许自环/有向边/带权：不要照抄本范例的 ensuref(u!=v) 与无向去重 key。
- edge_cases 名字与构造策略跟本题走，不要机械照搬 connected_tree/complete/path/star。
- generator.h 的 API 用法（.gen() / .edges()）可参考；输入格式与图语义不可照搬。

题面（范例题）：给定 n 个点 m 条边的无向图（无自环无重边），判断是否连通。
说明：树/链/菊花优先用 generator.h：先 .gen()，再 .edges()。稀疏随机边用 unordered_set 采样并设尝试上限。严禁枚举 O(n^2) 边池。
输入格式（本范例为单测）：第 1 行 n m；接下来 m 行每行 u v。
（若真实题目多测：先输出 T，再重复 T 组「n m + 边」——以标程为准。）
输出格式：YES 或 NO。
数据范围：n in [1,1000]，m in [0,min(3000,n*(n-1)/2)]，15 组。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 1000], "m": [0, 3000]},
  "edge_cases": ["connected_tree", "disconnected", "complete", "path", "star", "random_sparse", "edge_n1"]
}

gen.cpp:
#include "generator.h"
using namespace std;
using namespace generator::all;
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
// 无自环无向边；n=1 或无法再采时停止，禁止死循环
void sampleUndirected(int n, int m, vector<pair<int,int>>& edges) {
    if (n <= 1 || m <= 0) return;
    long long maxe = 1LL * n * (n - 1) / 2;
    if (m > maxe) m = (int)maxe;
    unordered_set<long long> used;
    used.reserve(m * 2);
    int tries = 0, lim = max(100, m * 40);
    while ((int)edges.size() < m && tries++ < lim) {
        int u = rnd.next(1, n), v = rnd.next(1, n);
        if (u == v) continue;
        if (u > v) swap(u, v);
        long long key = (long long)u * (n + 1) + v;
        if (!used.insert(key).second) continue;
        edges.push_back({u, v});
    }
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    int seed = opt<int>("seed");
    string typ = opt<string>("type", "random_sparse");
    (void)seed;
    int n = (typ == "edge_n1") ? 1 : max(1, pickSized(1, 1000));
    vector<pair<int,int>> edges;
    if (typ == "edge_n1") {
        // 本范例禁止自环：n=1 时 m 必须为 0
        n = 1;
    } else if (typ == "connected_tree" && n >= 2) {
        unweight::Tree t(n); t.gen();
        for (auto &e : t.edges()) edges.push_back({e.u(), e.v()});
    } else if (typ == "path" && n >= 2) {
        unweight::Chain t(n); t.gen();
        for (auto &e : t.edges()) edges.push_back({e.u(), e.v()});
    } else if (typ == "star" && n >= 2) {
        unweight::Flower t(n); t.gen();
        for (auto &e : t.edges()) edges.push_back({e.u(), e.v()});
    } else if (typ == "disconnected" && n >= 2) {
        int mid = max(1, n / 2);
        if (mid >= 2) {
            unweight::Tree a(mid); a.gen();
            for (auto &e : a.edges()) edges.push_back({e.u(), e.v()});
        }
        int right = n - mid;
        if (right >= 2) {
            unweight::Tree b(right); b.gen();
            for (auto &e : b.edges()) edges.push_back({e.u() + mid, e.v() + mid});
        }
    } else if (typ == "complete") {
        // 控制规模，避免 O(n^2) 超时；本题 m 上限 3000 → n≈77
        if (n > 70) n = 70;
        for (int u = 1; u <= n; u++)
            for (int v = u + 1; v <= n; v++)
                edges.push_back({u, v});
    } else {
        int m = (n <= 1) ? 0 : rnd.next(0, min(n, 3000));
        sampleUndirected(n, m, edges);
    }
    // 本范例单测：第一行 n m。多测题请先 printf T，再循环输出各组。
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
    int m = inf.readInt(0, 3000, "m"); inf.readEoln();
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


CPP_GEOMETRY_EXAMPLE = r"""
【参考范例：一道计算几何题的标准写法（C++ + ACM-generator）】
题面：给定平面上 n 个点，求凸包上的点数。
输入格式：第 1 行 n；接下来 n 行每行两个整数 x y。
输出格式：一个整数。
数据范围：n in [3,1000]，坐标 in [-1e6,1e6]，15 组，覆盖凸包、简单多边形顶点、随机点、共线、n=3。
说明：优先用 ConvexHull / SimplePolygon / RandomPoints；仍须 registerGen 与 CLI 契约。

range.json:
{
  "count": 15,
  "constraints": {"n": [3, 1000], "x": [-1000000, 1000000], "y": [-1000000, 1000000]},
  "edge_cases": ["convex_hull", "simple_polygon", "random_points", "collinear", "edge_n3"]
}

gen.cpp:
#include "generator.h"
using namespace std;
using namespace generator::all;
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
    string typ = opt<string>("type", "random_points");
    int n = (typ == "edge_n3") ? 3 : pickSized(3, 1000);
    int LIM = 1000000;
    if (typ == "convex_hull") {
        ConvexHull<int> c(n);
        c.set_xy_limit(-LIM, LIM);
        c.gen();
        cout << c << "\n";
    } else if (typ == "simple_polygon") {
        SimplePolygon<int> p(n);
        p.set_xy_limit(-LIM, LIM);
        p.gen();
        cout << p << "\n";
    } else if (typ == "collinear") {
        printf("%d\n", n);
        int y = rnd.next(-LIM, LIM);
        for (int i = 0; i < n; i++) {
            int x = rnd.next(-LIM, LIM);
            printf("%d %d\n", x, y);
        }
    } else {
        RandomPoints<int> pts(n, -LIM, LIM, -LIM, LIM);
        pts.gen();
        cout << pts << "\n";
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(3, 1000, "n");
    inf.readEoln();
    for (int i = 0; i < n; i++) {
        inf.readInt(-1000000, 1000000, "x"); inf.readSpace();
        inf.readInt(-1000000, 1000000, "y"); inf.readEoln();
    }
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


CPP_DP_EXAMPLE = r"""
【参考范例：一道 DP 题的标准写法（C++ testlib）】
题面：0-1 背包。给定容量 W 与 n 件物品的重量 wi、价值 vi，求最大价值。
输入格式：第 1 行 n W；接下来 n 行每行 wi vi。
输出格式：一个整数。
数据范围：n in [1,100]，W in [1,1000]，wi,vi in [1,1000]，15 组。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 100], "W": [1, 1000], "wi": [1, 1000], "vi": [1, 1000]},
  "edge_cases": ["edge_n1", "edge_nmax", "edge_W1", "edge_Wmax", "all_heavy"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0; if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt, b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a; if (a > hi) a = hi; if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random");
    int n = (typ == "edge_n1") ? 1 : (typ == "edge_nmax") ? 100 : pickSized(1, 100);
    int W = (typ == "edge_W1") ? 1 : (typ == "edge_Wmax") ? 1000 : pickSized(1, 1000);
    printf("%d %d\n", n, W);
    for (int i = 0; i < n; i++) {
        int w = (typ == "all_heavy") ? rnd.next(max(1, W), 1000) : rnd.next(1, 1000);
        int v = rnd.next(1, 1000);
        printf("%d %d\n", w, v);
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 100, "n"); inf.readSpace();
    int W = inf.readInt(1, 1000, "W"); inf.readEoln();
    for (int i = 0; i < n; i++) {
        inf.readInt(1, 1000, "wi"); inf.readSpace();
        inf.readInt(1, 1000, "vi"); inf.readEoln();
    }
    ensuref(n >= 1, "n>=1");
    inf.readEof();
    return 0;
}

"""

CPP_MATRIX_EXAMPLE = r"""
【参考范例：一道矩阵题的标准写法（C++ testlib）】
题面：给定 n×m 整数矩阵，输出所有元素之和。
输入格式：第 1 行 n m；接下来 n 行每行 m 个整数。
输出格式：一个整数。
数据范围：n,m in [1,200]，aij in [-1e9,1e9]，15 组。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 200], "m": [1, 200], "aij": [-1000000000, 1000000000]},
  "edge_cases": ["edge_11", "edge_nmax", "row", "col", "all_zero"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0; if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt, b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a; if (a > hi) a = hi; if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random");
    int n = 1, m = 1;
    if (typ == "edge_11") { n = m = 1; }
    else if (typ == "edge_nmax") { n = m = 200; }
    else if (typ == "row") { n = 1; m = pickSized(1, 200); }
    else if (typ == "col") { n = pickSized(1, 200); m = 1; }
    else { n = pickSized(1, 200); m = pickSized(1, 200); }
    printf("%d %d\n", n, m);
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < m; j++) {
            long long v = (typ == "all_zero") ? 0 : rnd.next(-1000000000LL, 1000000000LL);
            printf("%lld%c", v, j + 1 < m ? ' ' : '\n');
        }
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 200, "n"); inf.readSpace();
    int m = inf.readInt(1, 200, "m"); inf.readEoln();
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < m; j++) {
            inf.readLong(-1000000000LL, 1000000000LL, "aij");
            if (j + 1 < m) inf.readSpace();
        }
        inf.readEoln();
    }
    ensuref(n * m >= 1, "nonempty");
    inf.readEof();
    return 0;
}

"""

CPP_RANGE_QUERY_EXAMPLE = r"""
【参考范例：一道区间查询题的标准写法（C++ testlib）】
题面：给定长度为 n 的数组与 q 次询问，每次询问 [l,r] 的区间和。
输入格式：第 1 行 n q；第 2 行 n 个整数；接下来 q 行每行 l r（1-index）。
输出格式：q 行，每行一个整数。
数据范围：n,q in [1,1e5]，ai in [-1e9,1e9]，15 组。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 100000], "q": [1, 100000], "ai": [-1000000000, 1000000000]},
  "edge_cases": ["edge_n1", "edge_nmax", "q1", "qmax", "point_queries"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0; if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt, b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a; if (a > hi) a = hi; if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random");
    int n = (typ == "edge_n1") ? 1 : (typ == "edge_nmax") ? 100000 : pickSized(1, 100000);
    int q = (typ == "q1") ? 1 : (typ == "qmax") ? 100000 : pickSized(1, 100000);
    printf("%d %d\n", n, q);
    for (int i = 0; i < n; i++) printf("%lld%c", rnd.next(-1000000000LL, 1000000000LL), i + 1 < n ? ' ' : '\n');
    for (int i = 0; i < q; i++) {
        int l, r;
        if (typ == "point_queries") { l = r = rnd.next(1, n); }
        else { l = rnd.next(1, n); r = rnd.next(1, n); if (l > r) swap(l, r); }
        printf("%d %d\n", l, r);
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 100000, "n"); inf.readSpace();
    int q = inf.readInt(1, 100000, "q"); inf.readEoln();
    for (int i = 0; i < n; i++) {
        inf.readLong(-1000000000LL, 1000000000LL, "ai");
        if (i + 1 < n) inf.readSpace();
    }
    inf.readEoln();
    for (int i = 0; i < q; i++) {
        int l = inf.readInt(1, n, "l"); inf.readSpace();
        int r = inf.readInt(l, n, "r"); inf.readEoln();
        ensuref(l <= r, "l<=r");
    }
    inf.readEof();
    return 0;
}

"""

CPP_WEIGHTED_TREE_EXAMPLE = r"""
【参考范例：一道带权树题的标准写法（C++ + ACM-generator）】
题面：给定 n 个节点带边权的树，求边权和。
输入格式：第 1 行 n；接下来 n-1 行 u v w。
输出格式：一个整数。
数据范围：n in [2,1e5]，w in [1,1e9]，15 组，覆盖链、菊花、随机树。

range.json:
{
  "count": 15,
  "constraints": {"n": [2, 100000], "w": [1, 1000000000]},
  "edge_cases": ["chain", "star", "random_tree", "edge_n2", "edge_nmax"]
}

gen.cpp:
#include "generator.h"
using namespace std;
using namespace generator::all;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0; if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt, b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a; if (a > hi) a = hi; if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
auto wfn = []() { return rnd.next(1, 1000000000); };
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random_tree");
    int n = (typ == "edge_n2") ? 2 : (typ == "edge_nmax") ? 100000 : pickSized(2, 100000);
    if (typ == "chain") {
        edge_weight::Chain<int> t(n);
        t.set_edges_weight_function(wfn); t.gen(); cout << t << "\n";
    } else if (typ == "star") {
        edge_weight::Flower<int> t(n);
        t.set_edges_weight_function(wfn); t.gen(); cout << t << "\n";
    } else {
        edge_weight::Tree<int> t(n);
        t.set_edges_weight_function(wfn); t.gen(); cout << t << "\n";
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
const int MAXN = 100005; int par[MAXN];
int find(int x){return par[x]==x?x:par[x]=find(par[x]);}
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(2, 100000, "n"); inf.readEoln();
    for (int i = 1; i <= n; i++) par[i] = i;
    set<pair<int,int>> es;
    for (int i = 0; i < n - 1; i++) {
        int u = inf.readInt(1, n, "u"); inf.readSpace();
        int v = inf.readInt(1, n, "v"); inf.readSpace();
        inf.readLong(1, 1000000000LL, "w"); inf.readEoln();
        ensuref(u != v, "self loop");
        auto key = make_pair(min(u,v), max(u,v));
        ensuref(es.insert(key).second, "dup edge");
        int ru = find(u), rv = find(v);
        ensuref(ru != rv, "cycle");
        par[ru] = rv;
    }
    int r = find(1);
    for (int i = 2; i <= n; i++) ensuref(find(i) == r, "not connected");
    inf.readEof();
    return 0;
}

"""

CPP_WEIGHTED_GRAPH_EXAMPLE = r"""
【参考范例：一道带权图题的标准写法（C++ + ACM-generator）】
【仅供参考】下面范例是「单测、无向、无自环、正权」的示意。写本题 gen 必须以题面/标程/range.json/gen_plan 为准：
- 多测则先输出 T；有向/自环/负权则改采样与 validator，勿照抄本范例。
- edge_n1：本范例无自环 → n=1 时 m=0；若本题允许自环可改为 (1,1,w)。

题面（范例题）：给定 n 点 m 边带权无向图，输出边权之和。
输入格式（单测）：第 1 行 n m；接下来 m 行 u v w。
输出格式：一个整数。
数据范围：n in [1,1000]，m in [0,3000]，w in [1,1e9]，15 组。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 1000], "m": [0, 3000], "w": [1, 1000000000]},
  "edge_cases": ["connected_tree", "path", "star", "random_sparse", "edge_n1"]
}

gen.cpp:
#include "generator.h"
using namespace std;
using namespace generator::all;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0; if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt, b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a; if (a > hi) a = hi; if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random_sparse");
    int n = (typ == "edge_n1") ? 1 : pickSized(1, 1000);
    vector<tuple<int,int,long long>> edges;
    auto add_tree = [&](auto &t) {
        t.set_edges_weight_function([](){ return rnd.next(1, 1000000000); });
        t.gen();
        for (auto &e : t.edges()) edges.emplace_back(e.u(), e.v(), e.w());
    };
    if (typ == "edge_n1" || n <= 1) {
        n = 1; // 无自环约定下 m=0
    } else if (typ == "connected_tree" && n >= 2) {
        edge_weight::Tree<int> t(n); add_tree(t);
    } else if (typ == "path" && n >= 2) {
        edge_weight::Chain<int> t(n); add_tree(t);
    } else if (typ == "star" && n >= 2) {
        edge_weight::Flower<int> t(n); add_tree(t);
    } else {
        int m = rnd.next(0, min(n, 3000));
        unordered_set<long long> used;
        int tries = 0, lim = max(100, m * 40);
        while ((int)edges.size() < m && tries++ < lim) {
            int u = rnd.next(1, n), v = rnd.next(1, n);
            if (u == v) continue;
            if (u > v) swap(u, v);
            long long key = (long long)u * (n + 1) + v;
            if (!used.insert(key).second) continue;
            edges.emplace_back(u, v, rnd.next(1LL, 1000000000LL));
        }
    }
    // 单测示意；多测请先输出 T
    printf("%d %d\n", n, (int)edges.size());
    for (auto [u,v,w] : edges) printf("%d %d %lld\n", u, v, w);
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 1000, "n"); inf.readSpace();
    int m = inf.readInt(0, 3000, "m"); inf.readEoln();
    set<pair<int,int>> es;
    for (int i = 0; i < m; i++) {
        int u = inf.readInt(1, n, "u"); inf.readSpace();
        int v = inf.readInt(1, n, "v"); inf.readSpace();
        inf.readLong(1, 1000000000LL, "w"); inf.readEoln();
        ensuref(u != v, "self loop");
        ensuref(es.insert({min(u,v), max(u,v)}).second, "dup");
    }
    inf.readEof();
    return 0;
}

"""

CPP_INTERACTIVE_EXAMPLE = r"""
【参考范例：一道「查询交互」离线数据写法（C++ testlib）】
说明：本工具流水线是文件 I/O，不能跑真正的交互库。若题面是交互题但标程可改成「读完全部询问再答」，
可用本范例生成询问序列作为 .in；真正需要 interactor 的 OJ 交互请另配。
题面（离线版）：有隐藏数组 a[1..n]，先给定 n，再进行 q 次操作：1 i 询问 a[i]；最后输出所有询问答案之和（标程自带 a）。
输入格式：第 1 行 n q；第 2 行 n 个整数 a；接下来 q 行，每行两个整数 1 i。
输出格式：一个整数。
数据范围：n,q in [1,1e5]，15 组。

range.json:
{
  "count": 15,
  "constraints": {"n": [1, 100000], "q": [1, 100000], "ai": [1, 1000000000]},
  "edge_cases": ["edge_n1", "edge_nmax", "q1", "qmax", "repeat_ask"]
}

gen.cpp:
#include "testlib.h"
using namespace std;
int pickSized(int lo, int hi) {
    int idx = opt<int>("index", 0), cnt = opt<int>("count", 15);
    if (cnt <= 1) return rnd.next(lo, hi);
    if (idx < 0) idx = 0; if (idx >= cnt) idx = cnt - 1;
    long long span = (long long)hi - lo;
    long long a = lo + span * idx / cnt, b = lo + span * (idx + 1) / cnt;
    if (b < a) b = a; if (a > hi) a = hi; if (b > hi) b = hi;
    return rnd.next((int)a, (int)b);
}
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    string typ = opt<string>("type", "random");
    int n = (typ == "edge_n1") ? 1 : (typ == "edge_nmax") ? 100000 : pickSized(1, 100000);
    int q = (typ == "q1") ? 1 : (typ == "qmax") ? 100000 : pickSized(1, 100000);
    printf("%d %d\n", n, q);
    for (int i = 0; i < n; i++) printf("%d%c", rnd.next(1, 1000000000), i + 1 < n ? ' ' : '\n');
    int fixed = rnd.next(1, n);
    for (int i = 0; i < q; i++) {
        int idx = (typ == "repeat_ask") ? fixed : rnd.next(1, n);
        printf("1 %d\n", idx);
    }
    return 0;
}

validator.cpp:
#include "testlib.h"
using namespace std;
int main(int argc, char* argv[]) {
    registerValidation();
    int n = inf.readInt(1, 100000, "n"); inf.readSpace();
    int q = inf.readInt(1, 100000, "q"); inf.readEoln();
    for (int i = 0; i < n; i++) {
        inf.readInt(1, 1000000000, "ai");
        if (i + 1 < n) inf.readSpace();
    }
    inf.readEoln();
    for (int i = 0; i < q; i++) {
        int op = inf.readInt(1, 1, "op"); inf.readSpace();
        inf.readInt(1, n, "i"); inf.readEoln();
        ensuref(op == 1, "only query op=1 in this offline sample");
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
    "geometry": CPP_GEOMETRY_EXAMPLE,
    "multi_test": CPP_MULTI_TEST_EXAMPLE,
    "dp": CPP_DP_EXAMPLE,
    "matrix": CPP_MATRIX_EXAMPLE,
    "range_query": CPP_RANGE_QUERY_EXAMPLE,
    "weighted_tree": CPP_WEIGHTED_TREE_EXAMPLE,
    "weighted_graph": CPP_WEIGHTED_GRAPH_EXAMPLE,
    "interactive": CPP_INTERACTIVE_EXAMPLE,
}

