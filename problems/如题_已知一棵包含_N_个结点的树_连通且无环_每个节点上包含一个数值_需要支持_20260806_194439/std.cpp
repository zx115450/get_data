#include <bits/stdc++.h>
using namespace std;
typedef long long ll;
#define endl '\n'
#define int long long
typedef pair<int,int> pii;
int getlen (int x) {
	bitset<32> bt = x;
	for (int i=30;i>=0;i--) {
		if (bt[i] == 1) {
			return i;
		}
	}
	return -1;
}
void man() {
	int n;  cin>>n;
	vector<int> a(n+2);
	for (int i=1;i<=n;i++) {
		cin>>a[i];
	}
	vector<vector<int>> b(32 , vector<int>((1<<2), 0));
	for (int i=1;i<=n;i++) {
		bitset<32> bt2 = a[i];
		for (int j=0;j<=30;j++) {
			int x = (((j==0 ? 0 : bt2[j-1]))<<1) + (bt2[j]);
			b[j][x]++;
		}
	}
	int m; cin>>m;
	vector<pii> ps(m+1);
	for (int i=1;i<=m;i++) {
		int op,x; cin>>op>>x;
		ps[i] = {op , x};
	}
	for (int _i=1;_i<=m;_i++) {
		auto [op , x] = ps[_i];
		bitset<32> bt = x;
		for (int j=0;j<=30;j++) {
			int p1 = (j == 0) ? 0 : bt[j-1];
			int p2 = bt[j];
			int y = (p1<<1) + p2;
			if (op == 1) {
				vector<int> as((1<<2),0);
				for (int k=0;k<=(1<<2)-1;k++) {
					as[k&y] += b[j][k];
				}
				for (int k=0;k<=(1<<2)-1;k++) {
					b[j][k] = as[k];
				}
			} else if (op == 2) {
				vector<int> as((1<<2),0);
				for (int k=0;k<=(1<<2)-1;k++) {
					as[k|y] += b[j][k];
				}
				for (int k=0;k<=(1<<2)-1;k++) {
					b[j][k] = as[k];
				}
			} else {
				vector<int> as((1<<2),0);
				for (int k=0;k<=(1<<2)-1;k++) {
					as[k^y] += b[j][k];
				}
				for (int k=0;k<=(1<<2)-1;k++) {
					b[j][k] = as[k];
				}
			}
		}
		int ans = 0;
		for (int i=0;i<=30;i++) {
			ans += (b[i][2]);
		}
		cout<<ans<<endl;
	}
	
	
	
	
	
}
signed main() {
	ios::sync_with_stdio(false);
	cin.tie(0);
	int t = 1;
//	cin >> t;
	while (t--) man();
	return 0;
}

