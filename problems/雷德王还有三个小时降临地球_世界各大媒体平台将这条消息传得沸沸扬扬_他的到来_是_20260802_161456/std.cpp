#include<bits/stdc++.h>
using namespace std;
using ll=long long;
const ll inf=1e18;
int T;
void dijkstra(int s,int t,vector<ll>& dis,vector<vector<pair<ll,int>>>& g)
{
    priority_queue<pair<ll,int>,vector<pair<ll,int>>,greater<>> pq;
    pq.push({dis[s]=0,s});
    while(!pq.empty())
    {
        auto [cd,cx]=pq.top();pq.pop();
        if(cd>dis[cx]) continue;
        for(auto [w,nx]:g[cx])
        {
            if(dis[cx]+w<dis[nx])
            {
                dis[nx]=dis[cx]+w;
                pq.push({dis[nx],nx});
            }
        }
    }
}
int main()
{
    ios::sync_with_stdio(false),cin.tie(nullptr),cout.tie(nullptr);
    cin>>T;
    while(T--)
    {
        int n,k,s,t;cin>>n>>k>>s>>t;
        vector<vector<pair<ll,int>>> g(2*n+2);
        vector<ll> dis(2*n+2,inf);
        while(k--)
        {
            int p;cin>>p;
            g[p].push_back({0,p+n});
            g[p+n].push_back({0,p});
        }
        for(int i=1;i<n;i++)
        {
            int u,v;ll a,b;cin>>u>>v>>a>>b;
            g[u].push_back({a,v});
            g[v].push_back({a,u});
            g[u+n].push_back({b,v+n});
            g[v+n].push_back({b,u+n});
        }
        dijkstra(s, t,dis,g);
        cout<<min(dis[t],dis[t+n])<<"\n";
    }
    return 0;
}

/**
* @runId: 622172
* @language: C++ 20 With O2
* @author: 微风啊
* @submitTime: 2026-07-19 20:13:59
*/