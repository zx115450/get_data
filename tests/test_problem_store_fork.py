"""problem_store：换题检测 / 另存为新题。"""
from __future__ import annotations

from storage import problem_store as ps


def test_should_fork_on_different_titles():
    tree = {
        "statement": "如题，已知一棵包含 N 个结点的树（连通且无环），每个节点上包含一个数值。\n",
        "std_code": "int main(){}",
    }
    day = {
        "statement": "给定一个仅由小写英文字母组成的字符串 S。至少包含 day 和 gay。\n",
        "std_code": "int main(){}",
    }
    assert ps.should_fork_problem(tree, day) is True
    assert ps.should_fork_problem(tree, tree) is False
    assert ps.should_fork_problem(None, day) is False


def test_should_not_fork_minor_title_tweak():
    a = {"statement": "给定字符串 S，找出所有 day 与 gay 子串并拼接输出。\n\n细节。"}
    b = {"statement": "给定字符串 S，找出所有 day 与 gay 子串并拼接输出。\n\n更多细节补充。"}
    # 标题相同 → 指纹前缀也相同，不 fork
    assert ps.should_fork_problem(a, b) is False


def test_upsert_force_new_and_forbid_divergent(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROBLEMS_DIR", tmp_path)
    ws1 = {
        "statement": "题目甲：一棵树的路径求和。\n",
        "input_desc": "n m",
        "output_desc": "ans",
        "std_code": "int main(){return 0;}",
        "lang": "cpp",
        "problem_type": "tree",
        "title": "题目甲：一棵树的路径求和。",
    }
    meta1 = ps.upsert_problem(ws1, force_new=True)
    pid = meta1["id"]
    assert (tmp_path / pid / "statement.txt").is_file()

    ws2 = {
        "statement": "题目乙：统计字符串中的 abc 与 xyz。\n",
        "input_desc": "T",
        "output_desc": "s",
        "std_code": "int main(){return 0;}",
        "lang": "cpp",
        "problem_type": "string",
        "title": "题目乙：统计字符串中的 abc 与 xyz。",
        "id": pid,
    }
    meta2 = ps.upsert_problem(ws2, problem_id=pid, forbid_divergent_overwrite=True)
    assert meta2["id"] != pid
    # 旧题仍在
    old = ps.load_workspace_from_dir(tmp_path / pid, source="problem")
    assert old and "树" in old["statement"]
    new = ps.load_workspace_from_dir(tmp_path / meta2["id"], source="problem")
    assert new and "abc" in new["statement"]
