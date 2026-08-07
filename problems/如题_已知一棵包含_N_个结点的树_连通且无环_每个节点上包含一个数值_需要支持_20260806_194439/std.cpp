#include <bits/stdc++.h>
using namespace std;

// 标程：zx 找 day
// 算法：从左到右逐位置扫描字符串，遇到 "day" 或 "gay" 就拼接到答案末尾。
//       由于 day 和 gay 首字母不同（d vs g），不存在重叠，逐位置扫描即可。
// 时间复杂度：O(T * |S|)

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int T;
    cin >> T;
    while (T--) {
        string s;
        cin >> s;
        string ans;
        for (int i = 0; i + 3 <= (int)s.size(); i++) {
            if (s[i] == 'd' && s[i + 1] == 'a' && s[i + 2] == 'y') {
                ans += "day";
            } else if (s[i] == 'g' && s[i + 1] == 'a' && s[i + 2] == 'y') {
                ans += "gay";
            }
        }
        cout << ans << "\n";
    }
    return 0;
}

