#include <bits/stdc++.h>
using namespace std;
#define int long long
#define lb(x) ((x) & (-x))
#define fori(a, b) for (int i = a; i <= b; i++)
#define forj(a, b) for (int j = a; j <= b; j++)
#define for_(x, a, b) for (int x = a; x <= b; x++)
#define refi(a, b) for (int i = b; i >= a; i--)
#define all(a) a.begin(), a.end()
#define all1(a) a.begin() + 1, a.end()
#define getp(a, index) get<index>(a)
#define endl '\n'
#define grt greater<int>()
using ll = long long;
typedef pair<int, int> pii;
typedef vector<int> vi;
typedef vector<vector<int>> vii;
typedef tuple<int, int, int> piii;
typedef tuple<int, int, int, int> piiii;
typedef pair<double, double> pdd;
typedef long long ll;
typedef double db;
typedef unsigned long long ull;
typedef long double ldb;
template <typename T, typename... Args>
void print(T first, Args... args) {
	std::cout << first << " ";
	print(args...);
}
struct cmp {
	bool operator()(pii as, pii bs) {
		return as.second - as.first < bs.second - bs.first;
	}
};
const int N = 200010;
int mod = 998244353;
int fan[4][2] = {1, 0, 0, 1, -1, 0, 0, -1};
void man() {
		
	int n,m,k; cin>>n>>m>>k;
	vector<vector<int>> g(n+1);
	for (int i=1;i<=m;i++) {
		int u,v; cin>>u>>v;
		g[u].push_back(v);
		g[v].push_back(u);
	}
	vector<vector<int>> p(n+1 , vector<int>(2 , 1e18));
//	vector<int> sti(n+1);
	queue<piii> dq;
    p[1][0]=0;
	dq.push({1 , 0 , -1});
//	sti[1] = 1;
	while(!dq.empty()){
		auto [x , dist , pre] = dq.front();
		dq.pop();
		if (p[x][dist%2] < dist) {
			continue;
		}
		
		for (int y : g[x]) {
			if (y == pre) {
				continue;
			}
            int nd=dist+1;
            if(p[y][nd%2]>nd){
                p[y][nd%2]=nd;
                dq.push({y , dist + 1 , x});
            }
		}
	}
	cout<<0<<' ';
	for (int i=2;i<=n;i++) {
		ll res=1e18;
        if(p[i][0]!=1e18){
            ll x=(p[i][0]+k-1)/k*k;
            if((x-p[i][0])%2!=0){
                if(k&1){
                    x+=k;
                }
                else{
                    x=1e18;
                }
            }
            res=min(res,x);
        }
        if(p[i][1]!=1e18){
            ll x=(p[i][1]+k-1)/k*k;
            if((x-p[i][1])%2!=0){
                if(k&1){
                    x+=k;
                }
                else{
                    x=1e18;
                }
            }
            res=min(res,x);
        }
        if(res>=1e18) cout<<"-1 ";
        else cout<<res<<' ';
    }
    cout<<endl;
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