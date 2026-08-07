#include <bits/stdc++.h>
using namespace std;
#define int long long
#define endl '\n'
int a[1010][1010];
bool sti[1010][1010];
int fan[4][2] = {0,1,1,0,-1,0,0,-1};
vector<vector<pair<int,int>>> v(1000010,vector<pair<int,int>>());
signed main () {
	std::ios::sync_with_stdio(false);
	cin. tie(0),cout. tie(0);
	int n,m;
	cin>>n>>m;

	for (int i=1;i<=n;i++) {
		for (int k=1;k<=m;k++) {
			cin>>a[i][k];
			v[a[i][k]].push_back({i,k});
		}
	}
	int x1,y1,x2,y2;
	cin>>x1>>y1>>x2>>y2;
	
	deque<pair<int,pair<int,int>>> d;
	
	d.push_back({0,{x1,y1}});
	sti[x1][y1] = 1;
	int ans = -1;
	while (!d.empty()) {
		auto pp = d.front();
		d.pop_front();
		int step = pp.first; 
		auto p = pp.second;
		int x = p.first;
		int y = p.second;    
		if (a[x][y] == a[x2][y2]) {
			ans = step;
			break;
		}        
		vector<pair<int,int>> vs = v[a[x][y]];
		for (auto pl : vs) {
			int xx = pl.first;
			int yy = pl.second;
			for (int i=0;i<4;i++) {
				int as = xx+fan[i][0];
				int bs = yy+fan[i][1];
				if (as < 1 || as > n || bs < 1 || bs > m) continue;
				if (sti[as][bs] == 0) {                            	
					d.push_back({step+1,{as,bs}});
					sti[as][bs] = 1;
				}
			}
		}
	}
	
	cout<<ans<<endl;
	
	
	
	return 0;
}
//哇哇叫

/**
* @runId: 573508
* @language: C++ 17 With O2
* @author: 微风啊
* @submitTime: 2025-12-02 22:45:55
*/