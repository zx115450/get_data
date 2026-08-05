#include <bits/stdc++.h>
using namespace std;
#define int long long
#define endl '\n'
void man() {
	int n,m,k; cin>>n>>m>>k;
	vector<vector<int>> g(n+1);
	for (int i=1;i<=m;i++) {
		int u,v; cin>>u>>v;
		g[u].push_back(v);
		g[v].push_back(u);
	}
	set<int> st;
	vector<int> p(n+1);
	deque<int> dq;
	set<int> ans;
	vector<int> sti(n+1);
	for (int i=1;i<=k;i++) {
		int x; cin>>x;
		st.insert(x);
		dq.push_back(x);
	}
	
	while(!dq.empty()){
		int x = dq.front();
		dq.pop_front();
		if (sti[x] == 1) {
			continue;
		}
		sti[x] = 1;
		for (int y : g[x]) {
			if (sti[y]== 1) {
				continue;
			}
			if (!st.count(y)) ans.insert(y);
			p[y]++;
			if (!st.count(y) && p[y] >= 2) {
				dq.push_back(y);
			}
		}
	}
	
	cout<<ans.size()<<endl;
	for (int x : ans) {
		cout<<x<<' ';
	} cout<<endl;
	
	
	
	
}


signed main() {
	cout << fixed << setprecision(2);
	ios::sync_with_stdio(false);
	cin.tie(0);
	cout.tie(0);
	int T=1;
//	init();
	cin >> T;
	while (T--) {
		man();
	}
	return 0;
}	
