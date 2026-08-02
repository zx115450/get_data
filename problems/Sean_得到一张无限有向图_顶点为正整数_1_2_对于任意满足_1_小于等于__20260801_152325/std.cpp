#include <bits/stdc++.h>
using namespace std;
#define int long long
#define lb(x) ((x)&(-x))
#define fori(a,b) for (int i=a;i<=b;i++) 
#define forj(a,b) for (int j=a;j<=b;j++)
#define refi(a,b) for (int i=b;i>=a;i--)
#define all(a) a.begin(),a.end()
#define all1(a) a.begin()+1,a.end()
#define getp(a,index) get<index>(a)
#define endl '\n'
#define grt greater<int>()
typedef pair<int,int> pii;
typedef vector<int> vi;
typedef tuple<int,int,int> piii;
const int mod = 676767677;
void man() {
	int n,m; cin>>n>>m;
	vector<vector<pii>> vec(n+1);
	fori (1,m) {
		int x,y,w; cin>>x>>y>>w;
			if (w >= 0) {
				vec[x].push_back({w , y});
				vec[y].push_back({w , x});
			} else {
				vec[x].push_back({w , y});			
			}
	}
	
	queue<pii> q;
	vector<bool> sti(n+1);
	q.push({0,1});
	sti[1] = 1;
	vi cnt(n+1);
	vi dist(n+1 , 1e18);
	dist[1] = 0;
	while(!q.empty()){
		auto [w , index] = q.front(); q.pop();
		sti[index] = 0;
		
		for (auto [cost , x] : vec[index]) {
			if (dist[x] > dist[index] + cost) {
				dist[x] = dist[index] + cost;
				if (!sti[x]) {
					cnt[x]++;
					if (cnt[x] >= n) {
						cout<<"YES"<<endl;
						return;
					}
					q.push({dist[x] , x});
					sti[x] = 1;
				}
			}
		}
	}
	
	cout<<"NO"<<endl;
	
	
}	
signed main() {
	ios::sync_with_stdio(false);
	cin.tie(0);
	cout.tie(0);	
	int T = 1;
	cin >> T;
//	init();
	while (T--) {
		man();
	}
	return 0;
}