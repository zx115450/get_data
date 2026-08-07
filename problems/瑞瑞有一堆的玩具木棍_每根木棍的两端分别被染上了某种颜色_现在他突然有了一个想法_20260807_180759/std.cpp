#include<iostream>
#include<algorithm>
#include<cstdio>
#include<cstring>
#include<cmath>
#include<map>
#include<queue>
#include<vector>
#define IL inline
#define re register
#define LL long long
#define ULL unsigned long long
#define re register
#define debug printf("Now is %d\n",__LINE__);
using namespace std;

template<class T>inline void read(T&x)
{
    char ch=getchar();
    while(!isdigit(ch))ch=getchar();
    x=ch-'0';ch=getchar();
    while(isdigit(ch)){x=x*10+ch-'0';ch=getchar();}
}
inline int read()
{
	int x=0;
    char ch=getchar();
    while(!isdigit(ch))ch=getchar();
    x=ch-'0';ch=getchar();
    while(isdigit(ch)){x=x*10+ch-'0';ch=getchar();}
    return x;
}
int G[55];
template<class T>inline void write(T x)
{
    int g=0;
    if(x<0) x=-x,putchar('-');
    do{G[++g]=x%10;x/=10;}while(x);
    for(re int i=g;i>=1;--i)putchar('0'+G[i]);putchar('\n');
}



struct Trie
{
	int son[26],num;
}trie[2000010];
int n,trie_cnt;
int insert(string str)
{
	int now=0;
	for(int i=0;i<str.size();i++)
	{
		if(!trie[now].son[str[i]-'a']) trie[now].son[str[i]-'a']=++trie_cnt;
		now=trie[now].son[str[i]-'a'];
	}
	if(!trie[now].num) trie[now].num=++n;
	return trie[now].num;
}
int f[500010];
int getf(int x)
{
	while(x!=f[x]) x=f[x];
	return x;
}
void merge(int x,int y)
{
	x=getf(x);
	y=getf(y);
	f[x]=y;
}
int degree[500010];
int main()
{
	string str;
	int x,y;
	for(int i=1;i<=500000;i++) f[i]=i;
	while(cin>>str)
	{
		x=insert(str);
		cin>>str;
		y=insert(str);
		merge(x,y);
		degree[x]++;
		degree[y]++;
	}
	int flag=(degree[1]&1);
	for(int i=2;i<=n;i++)
	{
		if(getf(i)!=getf(i-1)) flag+=10;
		if(degree[i]&1) flag++;
	}
	if(flag>2) cout<<"Impossible"<<endl;
	else cout<<"Possible"<<endl;
	return 0;
}

