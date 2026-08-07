	#include <bits/stdc++.h>
	using namespace std;
	#define int long long
	#define endl '\n'
	int n;
	int a[10010];
	int b[5005];
	void man() {
		cin>>n;
		for (int i=1;i<=n;i++) {
			cin>>a[i];
		}
		sort(a+1,a+1+n);
		for (int i=1;i<=n;i++) {
			if (!(i&1)) {
				int l=1;
				int r=i;
				int mx = 0;
				while (l<=r) {
					mx = max(a[l] + a[r],mx);
					l++;
					r--;
				}
				b[i/2] = mx;
			}	
		}
		int m;
		cin>>m;
		for (int is=1;is<=m;is++) {
			int k;
			cin>>k;
			int l=1;
			int r=n/2;
			int ans = 0;
			while (l<=r) {
				int mid = l+r>>1;
				if (b[mid] > k) {
					r = mid-1;
				} else {
					l = mid+1;
					ans = mid;
				}
			}
				cout<<ans*2<<endl;
		}
	}
	signed main () {
		std::ios::sync_with_stdio(false);
		cin. tie(0),cout. tie(0);
		int t;
		cin>>t;
		while (t--) {
			man();
		}
		return 0;
	}