"""英文→片假名近似转换 + JP 真实姓名（漢字 + 片假名）抽样池。

PayPal JP guest 表单要求 `#firstName` / `#lastName`（漢字）和
`#countrySpecificFirstName` / `#countrySpecificLastName`（片假名）**字面对应**，
否则 PayPal 风控会判"片假名跟漢字不匹配"导致提交失败。

所以 JP 模式下不要用英文卡名硬转片假名，应直接从下面这两个池里各抽一对。
卡的持卡人姓名（卡上印的英文）跟 PayPal 账户姓名不必相同。
"""
from __future__ import annotations

import secrets

# 姓（漢字, 片假名）—— 来自 aBaiAutoplus 的 JP_LAST_NAMES
JP_LAST_NAMES = (
    ("佐藤", "サトウ"),
    ("鈴木", "スズキ"),
    ("高橋", "タカハシ"),
    ("田中", "タナカ"),
    ("伊藤", "イトウ"),
    ("渡辺", "ワタナベ"),
    ("山本", "ヤマモト"),
    ("中村", "ナカムラ"),
    ("小林", "コバヤシ"),
    ("加藤", "カトウ"),
    ("吉田", "ヨシダ"),
    ("山田", "ヤマダ"),
    ("佐々木", "ササキ"),
    ("山口", "ヤマグチ"),
    ("松本", "マツモト"),
    ("井上", "イノウエ"),
    ("木村", "キムラ"),
    ("林", "ハヤシ"),
    ("清水", "シミズ"),
    ("山崎", "ヤマザキ"),
)

# 名（漢字, 片假名, 性别）—— 性别仅参考
JP_GIVEN_NAMES = (
    ("翔太", "ショウタ", "M"),
    ("大輔", "ダイスケ", "M"),
    ("健太", "ケンタ", "M"),
    ("拓也", "タクヤ", "M"),
    ("悠真", "ユウマ", "M"),
    ("蓮", "レン", "M"),
    ("陽翔", "ハルト", "M"),
    ("海斗", "カイト", "M"),
    ("直樹", "ナオキ", "M"),
    ("一郎", "イチロウ", "M"),
    ("美咲", "ミサキ", "F"),
    ("葵", "アオイ", "N"),
    ("結衣", "ユイ", "F"),
    ("陽菜", "ヒナ", "F"),
    ("凛", "リン", "F"),
    ("愛莉", "アイリ", "F"),
    ("美月", "ミツキ", "F"),
    ("花音", "カノン", "F"),
    ("真央", "マオ", "F"),
    ("七海", "ナナミ", "F"),
)


def random_jp_name() -> dict:
    """随机抽一对日本姓名（漢字+片假名同源）。

    ★★ 内部委托给 jp_identity 模块（保持池统一），失败时回退本地 fallback 池。

    Returns:
        {
            "first_kanji": "翔太",
            "first_kana":  "ショウタ",
            "last_kanji":  "佐藤",
            "last_kana":   "サトウ",
        }
    """
    try:
        # 优先用 jp_identity（跟 webui「JP 资料」按钮和 paypal 自动化共享同一个池）
        from jp_identity import (
            JP_LAST_NAMES as _JP_LAST,
            JP_FIRST_NAMES as _JP_FIRST,
        )
        last_kanji, last_kana, _ = secrets.choice(_JP_LAST)
        first_kanji, first_kana, _romaji, _gender = secrets.choice(_JP_FIRST)
        return {
            "first_kanji": first_kanji,
            "first_kana": first_kana,
            "last_kanji": last_kanji,
            "last_kana": last_kana,
        }
    except Exception:
        # 退回本地 fallback 池（没漢字的旧 schema 不可能走到这里，但保留）
        last_kanji, last_kana = secrets.choice(JP_LAST_NAMES)
        first_kanji, first_kana, _ = secrets.choice(JP_GIVEN_NAMES)
        return {
            "first_kanji": first_kanji,
            "first_kana": first_kana,
            "last_kanji": last_kanji,
            "last_kana": last_kana,
        }


# ---------- 旧的英文→片假名转换（已弃用，保留向后兼容） ----------

# 罗马字 → 片假名（大段优先匹配）
_ROMAJI_KANA = [
    ("kkya", "ッキャ"), ("kkyu", "ッキュ"), ("kkyo", "ッキョ"),
    ("shi", "シ"), ("chi", "チ"), ("tsu", "ツ"),
    ("kya", "キャ"), ("kyu", "キュ"), ("kyo", "キョ"),
    ("sha", "シャ"), ("shu", "シュ"), ("sho", "ショ"),
    ("cha", "チャ"), ("chu", "チュ"), ("cho", "チョ"),
    ("nya", "ニャ"), ("nyu", "ニュ"), ("nyo", "ニョ"),
    ("hya", "ヒャ"), ("hyu", "ヒュ"), ("hyo", "ヒョ"),
    ("mya", "ミャ"), ("myu", "ミュ"), ("myo", "ミョ"),
    ("rya", "リャ"), ("ryu", "リュ"), ("ryo", "リョ"),
    ("gya", "ギャ"), ("gyu", "ギュ"), ("gyo", "ギョ"),
    ("ja", "ジャ"), ("ju", "ジュ"), ("jo", "ジョ"),
    ("bya", "ビャ"), ("byu", "ビュ"), ("byo", "ビョ"),
    ("pya", "ピャ"), ("pyu", "ピュ"), ("pyo", "ピョ"),
    ("ka", "カ"), ("ki", "キ"), ("ku", "ク"), ("ke", "ケ"), ("ko", "コ"),
    ("sa", "サ"), ("su", "ス"), ("se", "セ"), ("so", "ソ"),
    ("ta", "タ"), ("te", "テ"), ("to", "ト"),
    ("na", "ナ"), ("ni", "ニ"), ("nu", "ヌ"), ("ne", "ネ"), ("no", "ノ"),
    ("ha", "ハ"), ("hi", "ヒ"), ("fu", "フ"), ("he", "ヘ"), ("ho", "ホ"),
    ("ma", "マ"), ("mi", "ミ"), ("mu", "ム"), ("me", "メ"), ("mo", "モ"),
    ("ya", "ヤ"), ("yu", "ユ"), ("yo", "ヨ"),
    ("ra", "ラ"), ("ri", "リ"), ("ru", "ル"), ("re", "レ"), ("ro", "ロ"),
    ("wa", "ワ"), ("wo", "ヲ"),
    ("ga", "ガ"), ("gi", "ギ"), ("gu", "グ"), ("ge", "ゲ"), ("go", "ゴ"),
    ("za", "ザ"), ("ji", "ジ"), ("zu", "ズ"), ("ze", "ゼ"), ("zo", "ゾ"),
    ("da", "ダ"), ("de", "デ"), ("do", "ド"),
    ("ba", "バ"), ("bi", "ビ"), ("bu", "ブ"), ("be", "ベ"), ("bo", "ボ"),
    ("pa", "パ"), ("pi", "ピ"), ("pu", "プ"), ("pe", "ペ"), ("po", "ポ"),
    ("th", "ス"), ("ch", "チ"), ("sh", "シ"), ("ph", "フ"), ("ck", "ック"),
    ("a", "ア"), ("i", "イ"), ("u", "ウ"), ("e", "エ"), ("o", "オ"),
    ("k", "ク"), ("s", "ス"), ("t", "ト"), ("n", "ン"), ("h", "フ"),
    ("m", "ム"), ("y", "イ"), ("r", "ル"), ("w", "ウ"),
    ("g", "グ"), ("z", "ズ"), ("d", "ド"), ("b", "ブ"), ("p", "プ"),
    ("f", "フ"), ("v", "ブ"), ("l", "ル"), ("c", "ク"), ("j", "ジ"),
    ("q", "ク"), ("x", "クス"),
]


def romaji_to_katakana(name: str) -> str:
    """英文名 → 片假名（粗略转换）。已弃用，新代码用 random_jp_name()。"""
    s = (name or "").lower()
    out = []
    i = 0
    while i < len(s):
        if not s[i].isalpha():
            i += 1
            continue
        matched = False
        for romaji, kana in _ROMAJI_KANA:
            if s.startswith(romaji, i):
                out.append(kana)
                i += len(romaji)
                matched = True
                break
        if not matched:
            i += 1
    result = "".join(out)
    return result or "タロウ"


def english_to_kana_pair(first_name: str, last_name: str) -> tuple[str, str]:
    """已弃用，新代码用 random_jp_name()。"""
    f = romaji_to_katakana(first_name) or "タロウ"
    l = romaji_to_katakana(last_name) or "ヤマダ"
    return f, l


if __name__ == "__main__":
    for _ in range(3):
        print(random_jp_name())
