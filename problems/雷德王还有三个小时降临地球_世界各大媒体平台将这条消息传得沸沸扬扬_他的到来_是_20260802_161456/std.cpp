#include <bits/stdc++.h>
using namespace std;

using i64 = long long;
const i64 INF = -1; // 表示不存在，输出用 -1

// 返回 g(p,k)；不存在返回 -1
i64 calc_g(i64 p, i64 k) {
	// x0 = ceil(p/k)*k
	i64 x0;
	if (p == 0) {
		x0 = 0;
	} else {
		// (p+k-1)/k * k，注意防溢出用 __int128
		__int128 num = (__int128)p + k - 1;
		x0 = (i64)((num / k) * k);
	}

	if (((x0 - p) & 1) == 0) {
		return x0;
	}
	// x0-p 为奇数
	if (k & 1) {
		// 再加一个 k，可能溢出到 > 2^63-1？x0+k ≤ (p+k-1)+k < 3e18，仍在 i64 内
		return x0 + k;
	}
	return -1;
}

i64 solve_one(i64 p0, i64 p1, i64 k) {
	i64 ans = -1;
	auto upd = [&](i64 p) {
		if (p < 0) return; // -1 表示不存在
		i64 g = calc_g(p, k);
		if (g < 0) return;
		if (ans < 0 || g < ans) ans = g;
	};
	upd(p0);
	upd(p1);
	return ans;
}

int main() {
	ios::sync_with_stdio(false);
	cin.tie(nullptr);

	int T;
	cin >> T;
	while (T--) {
		i64 p0, p1, k;
		cin >> p0 >> p1 >> k;
		cout << solve_one(p0, p1, k) << '\n';
	}
	return 0;
}
