"""日本资料一键生成器（PayPal / OpenAI 注册用）。

按截图格式生成：
  - 姓 (片假名) + 名 (片假名) + 完整姓名
  - 出生日期 (YYYY/MM/DD)
  - 邮编 + 都道府県 + 市区町村 + 街道地址 + 完整地址
  - 卡号（默认 JCB / Visa）+ 卡 BIN + 有效期 + CVV
  - 邮箱（罗马字小写 + 出生年后两位 + @gmail.com）
  - 强密码

所有数据来自真实存在的日本邮编 / 街道 / 卡 BIN 池，避免随便编触发风控。
"""
from __future__ import annotations

import random
import string
from typing import Any


# =============================================================================
# 日本姓 (漢字, 片假名, 罗马字)
# =============================================================================
# 漢字 + 片假名同源（PayPal hosted JP 表单要求 #firstName/#lastName 漢字版
# 和 #countrySpecificFirstName/Last 片假名版字面对应，否则风控判匹配失败）
JP_LAST_NAMES: tuple[tuple[str, str, str], ...] = (
    ("佐藤", "サトウ", "Sato"),
    ("鈴木", "スズキ", "Suzuki"),
    ("高橋", "タカハシ", "Takahashi"),
    ("田中", "タナカ", "Tanaka"),
    ("伊藤", "イトウ", "Ito"),
    ("渡辺", "ワタナベ", "Watanabe"),
    ("山本", "ヤマモト", "Yamamoto"),
    ("中村", "ナカムラ", "Nakamura"),
    ("小林", "コバヤシ", "Kobayashi"),
    ("加藤", "カトウ", "Kato"),
    ("吉田", "ヨシダ", "Yoshida"),
    ("山田", "ヤマダ", "Yamada"),
    ("佐々木", "ササキ", "Sasaki"),
    ("山口", "ヤマグチ", "Yamaguchi"),
    ("松本", "マツモト", "Matsumoto"),
    ("井上", "イノウエ", "Inoue"),
    ("木村", "キムラ", "Kimura"),
    ("林", "ハヤシ", "Hayashi"),
    ("清水", "シミズ", "Shimizu"),
    ("山崎", "ヤマザキ", "Yamazaki"),
    ("森", "モリ", "Mori"),
    ("阿部", "アベ", "Abe"),
    ("池田", "イケダ", "Ikeda"),
    ("橋本", "ハシモト", "Hashimoto"),
    ("山下", "ヤマシタ", "Yamashita"),
    ("石川", "イシカワ", "Ishikawa"),
    ("中島", "ナカジマ", "Nakajima"),
    ("前田", "マエダ", "Maeda"),
    ("藤田", "フジタ", "Fujita"),
    ("小川", "オガワ", "Ogawa"),
    ("後藤", "ゴトウ", "Goto"),
    ("岡田", "オカダ", "Okada"),
    ("長谷川", "ハセガワ", "Hasegawa"),
    ("村上", "ムラカミ", "Murakami"),
    ("近藤", "コンドウ", "Kondo"),
    ("石井", "イシイ", "Ishii"),
    ("酒井", "サカイ", "Sakai"),
    ("遠藤", "エンドウ", "Endo"),
    ("青木", "アオキ", "Aoki"),
    ("藤井", "フジイ", "Fujii"),
)


# =============================================================================
# 日本名 (漢字, 片假名, 罗马字, 性别)
# =============================================================================
JP_FIRST_NAMES: tuple[tuple[str, str, str, str], ...] = (
    # 男性
    ("翔太", "ショウタ", "Shota", "M"),
    ("大輔", "ダイスケ", "Daisuke", "M"),
    ("健太", "ケンタ", "Kenta", "M"),
    ("拓也", "タクヤ", "Takuya", "M"),
    ("悠真", "ユウマ", "Yuma", "M"),
    ("蓮", "レン", "Ren", "M"),
    ("陽翔", "ハルト", "Haruto", "M"),
    ("海斗", "カイト", "Kaito", "M"),
    ("直樹", "ナオキ", "Naoki", "M"),
    ("一郎", "イチロウ", "Ichiro", "M"),
    ("広志", "ヒロシ", "Hiroshi", "M"),
    ("健司", "ケンジ", "Kenji", "M"),
    ("勝", "マサル", "Masaru", "M"),
    ("孝", "タカシ", "Takashi", "M"),
    ("次郎", "ジロウ", "Jiro", "M"),
    ("雄二", "ユウジ", "Yuji", "M"),
    ("竜太", "リュウタ", "Ryuta", "M"),
    ("颯太", "ソウタ", "Sota", "M"),
    ("優希", "ユウキ", "Yuki", "M"),
    ("正宏", "マサヒロ", "Masahiro", "M"),
    # 女性
    ("美咲", "ミサキ", "Misaki", "F"),
    ("結衣", "ユイ", "Yui", "F"),
    ("陽菜", "ヒナ", "Hina", "F"),
    ("凛", "リン", "Rin", "F"),
    ("愛莉", "アイリ", "Airi", "F"),
    ("美月", "ミツキ", "Mitsuki", "F"),
    ("花音", "カノン", "Kanon", "F"),
    ("真央", "マオ", "Mao", "F"),
    ("七海", "ナナミ", "Nanami", "F"),
    ("莉奈", "リナ", "Rina", "F"),
    ("桜", "サクラ", "Sakura", "F"),
    ("花子", "ハナコ", "Hanako", "F"),
    ("優香", "ユカ", "Yuka", "F"),
    ("彩", "アヤ", "Aya", "F"),
    ("芽衣", "メイ", "Mei", "F"),
    ("遥", "ハルカ", "Haruka", "F"),
    ("千夏", "チナツ", "Chinatsu", "F"),
    ("優奈", "ユウナ", "Yuna", "F"),
    ("希", "ノゾミ", "Nozomi", "F"),
    ("美雪", "ミユキ", "Miyuki", "F"),
    # 中性
    ("葵", "アオイ", "Aoi", "N"),
)


# =============================================================================
# 日本邮编 + 都道府県 + 市区町村 + 街道地址池
# =============================================================================
# 格式: (邮编, 都道府県英文, 市区町村英文, [街道模板...])
# 街道模板支持 {n1}-{n2}-{n3} 占位符，n1=1-9, n2=1-30, n3=1-30
JP_ADDRESSES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # 北海道
    ("060-0001", "Hokkaido", "Sapporo", ("Kita 1 Jo Nishi", "Odori Nishi", "Ginza")),
    ("060-0051", "Hokkaido", "Sapporo", ("Minami 1 Jo Higashi", "Ginza", "Susukino")),
    ("064-0809", "Hokkaido", "Sapporo", ("Minami 9 Jo Nishi", "Hassamu", "Maruyama")),
    ("080-0010", "Hokkaido", "Obihiro", ("Higashi 10 Jo Minami", "Nishi 5 Jo")),
    # 東京都
    ("100-0001", "Tokyo", "Chiyoda", ("Chiyoda", "Marunouchi")),
    ("100-0005", "Tokyo", "Chiyoda", ("Marunouchi", "Otemachi")),
    ("104-0061", "Tokyo", "Chuo", ("Ginza", "Kyobashi")),
    ("105-0011", "Tokyo", "Minato", ("Shibakoen", "Hamamatsucho")),
    ("106-0032", "Tokyo", "Minato", ("Roppongi", "Nishiazabu")),
    ("150-0002", "Tokyo", "Shibuya", ("Shibuya", "Dogenzaka")),
    ("150-0042", "Tokyo", "Shibuya", ("Udagawacho", "Maruyamacho")),
    ("160-0022", "Tokyo", "Shinjuku", ("Shinjuku", "Kabukicho")),
    ("171-0014", "Tokyo", "Toshima", ("Ikebukuro", "Mejiro")),
    ("130-0013", "Tokyo", "Sumida", ("Kinshi", "Honjo")),
    # 大阪府
    ("530-0001", "Osaka", "Kita", ("Umeda", "Sonezaki")),
    ("542-0076", "Osaka", "Chuo", ("Namba", "Dotonbori")),
    ("550-0014", "Osaka", "Nishi", ("Kitahorie", "Minamihorie")),
    ("556-0011", "Osaka", "Naniwa", ("Nipponbashi", "Ebisu Higashi")),
    # 愛知県
    ("460-0008", "Aichi", "Naka", ("Sakae", "Nishiki")),
    ("450-0002", "Aichi", "Nakamura", ("Meieki", "Nakamura")),
    ("464-0819", "Aichi", "Chikusa", ("Chikusa", "Imaike")),
    # 神奈川県
    ("220-0011", "Kanagawa", "Yokohama", ("Takashima", "Minatomirai")),
    ("231-0023", "Kanagawa", "Yokohama", ("Yamashitacho", "Honcho")),
    ("210-0007", "Kanagawa", "Kawasaki", ("Ekimaehoncho", "Ogawacho")),
    # 福岡県
    ("812-0011", "Fukuoka", "Hakata", ("Hakataekimae", "Sumiyoshi")),
    ("810-0001", "Fukuoka", "Chuo", ("Tenjin", "Daimyo")),
    # 京都府
    ("604-0901", "Kyoto", "Nakagyo", ("Karasuma", "Kawaramachi")),
    ("605-0073", "Kyoto", "Higashiyama", ("Gion", "Sakyo")),
    # 兵庫県
    ("650-0021", "Hyogo", "Chuo", ("Sannomiya", "Motomachi")),
    ("662-0833", "Hyogo", "Nishinomiya", ("Mondoyakujin", "Hirota")),
    # 千葉県
    ("260-0013", "Chiba", "Chuo", ("Chuoko", "Hisamoto")),
    # 埼玉県
    ("330-0853", "Saitama", "Omiya", ("Sakuragicho", "Higashicho")),
    # 宮城県
    ("980-0021", "Miyagi", "Sendai", ("Chuo", "Ichibancho")),
    # 静岡県
    ("420-0034", "Shizuoka", "Aoi", ("Tokiwacho", "Goyu")),
    # 広島県
    ("730-0011", "Hiroshima", "Naka", ("Motomachi", "Kamiyacho")),
    # 沖縄県
    ("900-0014", "Okinawa", "Naha", ("Matsuo", "Kumoji")),
    # 福島県
    ("960-8031", "Fukushima", "Fukushima", ("Sakaemachi", "Hanazonocho")),
    # 新潟県
    ("951-8067", "Niigata", "Chuo", ("Furumachi", "Honcho")),
    # 茨城県
    ("310-0011", "Ibaraki", "Mito", ("Sannomaru", "Izumicho")),
    # 群馬県
    ("371-0023", "Gunma", "Maebashi", ("Hinatamachi", "Honcho")),
    # 栃木県
    ("320-0026", "Tochigi", "Utsunomiya", ("Bansuicho", "Hommachi")),
)


# =============================================================================
# JCB / Visa 真实发卡行 BIN 池（截图里的 414709 是 Rakuten Bank Visa Debit）
# =============================================================================
# 格式: (BIN 6位, 品牌)
JP_CARD_BINS: tuple[tuple[str, str], ...] = (
    # JCB（日本最大发卡组织）
    ("352800", "JCB"),
    ("352831", "JCB"),
    ("353070", "JCB"),
    ("354000", "JCB"),
    ("354020", "JCB"),
    ("354054", "JCB"),
    ("352836", "JCB"),
    ("358910", "JCB"),
    # Visa（日本主流，截图里的 414709 = Rakuten Bank Visa Debit）
    ("414709", "Visa"),  # Rakuten Bank Visa Debit
    ("447629", "Visa"),  # Mizuho Visa
    ("453011", "Visa"),  # Sumitomo Mitsui Visa
    ("455736", "Visa"),  # Sony Bank Visa
    ("476126", "Visa"),  # SBI Sumishin Net Bank Visa
    # Mastercard
    ("521756", "Mastercard"),
    ("539821", "Mastercard"),
)


# =============================================================================
# 生成器
# =============================================================================
def _luhn_check_digit(digits: str) -> str:
    """Luhn 校验位：给前 N-1 位算最后一位。"""
    s = 0
    rev = digits[::-1]
    for i, ch in enumerate(rev):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return str((10 - s % 10) % 10)


def _generate_card_number(bin_prefix: str, total_len: int = 16) -> str:
    """生成一个 Luhn 通过的卡号。"""
    mid_len = total_len - len(bin_prefix) - 1
    mid = "".join(random.choices("0123456789", k=mid_len))
    head = bin_prefix + mid
    return head + _luhn_check_digit(head)


def _format_card_groups(card_no: str) -> str:
    """16 位卡号格式化成 4-4-4-4 分组。"""
    return " ".join(card_no[i:i+4] for i in range(0, len(card_no), 4))


def _gen_dob(min_age: int = 22, max_age: int = 55) -> str:
    """随机出生日期 YYYY/MM/DD（22-55 岁）。"""
    from datetime import date
    today = date.today()
    age = random.randint(min_age, max_age)
    year = today.year - age
    month = random.randint(1, 12)
    # 安全 day 范围：1-28（避免 2 月 29 / 31 这些边界）
    day = random.randint(1, 28)
    return f"{year}/{month:02d}/{day:02d}"


def _gen_expiry(years_ahead_min: int = 2, years_ahead_max: int = 6) -> str:
    """生成有效期 MM/YY（未来 2-6 年）。"""
    from datetime import date
    today = date.today()
    year = today.year + random.randint(years_ahead_min, years_ahead_max)
    month = random.randint(1, 12)
    return f"{month:02d}/{str(year)[-2:]}"


def _gen_password(length: int = 12) -> str:
    """生成强密码：含大小写字母 + 数字 + 至少 1 个特殊符号，开头是符号或字母。"""
    pool_lower = string.ascii_lowercase
    pool_upper = string.ascii_uppercase
    pool_digit = string.digits
    pool_symbol = "!@#$%&*"
    # 至少各 1 个，剩余从全池抽
    need = (
        random.choice(pool_symbol),
        random.choice(pool_lower),
        random.choice(pool_upper),
        random.choice(pool_digit),
    )
    rest_len = max(0, length - len(need))
    rest_pool = pool_lower + pool_upper + pool_digit
    rest = random.choices(rest_pool, k=rest_len)
    chars = list(need) + list(rest)
    random.shuffle(chars)
    # 截图里密码以 ! 开头，这里也保证开头是符号或大写字母（视觉对齐）
    if chars[0] in pool_lower + pool_digit:
        # 把第一个换成大写或符号
        for i in range(1, len(chars)):
            if chars[i] in pool_upper + pool_symbol:
                chars[0], chars[i] = chars[i], chars[0]
                break
    return "".join(chars)


def _gen_email(first_romaji: str, last_romaji: str, dob: str) -> str:
    """生成 gmail 邮箱：rinamatsumoto93@gmail.com 风格。"""
    yy = dob.split("/")[0][-2:] if "/" in dob else "00"
    # 5% 概率没数字后缀
    suffix = "" if random.random() < 0.05 else yy
    # 1/3 概率额外加 1-2 位随机数字
    if random.random() < 0.33:
        suffix += "".join(random.choices("0123456789", k=random.randint(1, 2)))
    base = f"{first_romaji}{last_romaji}{suffix}".lower()
    domain = random.choice(("gmail.com", "gmail.com", "gmail.com", "outlook.com", "yahoo.co.jp"))
    return f"{base}@{domain}"


def _gen_street_address(street_template: str) -> str:
    """街道地址：'Ginza 1-29-11' / 'Shibuya 2-3-4'"""
    n1 = random.randint(1, 9)
    n2 = random.randint(1, 30)
    n3 = random.randint(1, 30)
    return f"{street_template} {n1}-{n2}-{n3}"


def generate_jp_identity(
    *,
    gender: str = "any",
    card_brand: str = "any",
) -> dict[str, Any]:
    """一键生成日本资料。

    Args:
        gender: "M" / "F" / "N" / "any"
        card_brand: "JCB" / "Visa" / "Mastercard" / "any"
    """
    # 姓 + 名（漢字 + 片假名 + 罗马字 同源）
    last_kanji, last_kana, last_romaji = random.choice(JP_LAST_NAMES)
    if gender in {"M", "F", "N"}:
        candidates = [n for n in JP_FIRST_NAMES if n[3] == gender]
        if not candidates:
            candidates = list(JP_FIRST_NAMES)
    else:
        candidates = list(JP_FIRST_NAMES)
    first_kanji, first_kana, first_romaji, sex = random.choice(candidates)

    # 地址
    postal, prefecture, city, street_pool = random.choice(JP_ADDRESSES)
    street = _gen_street_address(random.choice(street_pool))
    full_address = f"〒{postal} {prefecture} {city} {street}"

    # 出生日期
    dob = _gen_dob()

    # 卡号
    if card_brand in {"JCB", "Visa", "Mastercard"}:
        bins = [b for b in JP_CARD_BINS if b[1] == card_brand]
        if not bins:
            bins = list(JP_CARD_BINS)
    else:
        bins = list(JP_CARD_BINS)
    bin_prefix, brand = random.choice(bins)
    card_number = _generate_card_number(bin_prefix, total_len=16)
    card_number_formatted = _format_card_groups(card_number)
    card_expiry = _gen_expiry()
    card_cvv = f"{random.randint(0, 999):03d}"

    # 邮箱 + 密码
    email = _gen_email(first_romaji, last_romaji, dob)
    password = _gen_password(12)

    return {
        # 姓名（漢字 + 片假名 + 罗马字 三套都给，paypal hosted JP 表单按 id 取用）
        "last_name_kanji": last_kanji,
        "last_name_kana": last_kana,
        "last_name_romaji": last_romaji,
        "first_name_kanji": first_kanji,
        "first_name_kana": first_kana,
        "first_name_romaji": first_romaji,
        "full_name_kanji": f"{last_kanji} {first_kanji}",
        "full_name_kana": f"{last_kana} {first_kana}",
        "full_name_romaji": f"{first_romaji} {last_romaji}",
        "gender": sex,
        # 出生
        "date_of_birth": dob,
        # 地址
        "postal_code": postal,
        "prefecture": prefecture,
        "city": city,
        "street": street,
        "full_address": full_address,
        # 卡
        "card_number": card_number,
        "card_number_formatted": card_number_formatted,
        "card_bin": bin_prefix,
        "card_brand": brand,
        "card_expiry": card_expiry,
        "card_cvv": card_cvv,
        # 账号
        "email": email,
        "password": password,
    }


def generate_jp_identity_for_paypal(
    *,
    gender: str = "any",
    card_brand: str = "JCB",
) -> dict[str, Any]:
    """生成 paypal hosted JP guest 表单需要的完整资料（漢字+片假名同源）。

    返回字段名跟 paypal_protocol / pipeline.py 里的 identity dict 兼容：
        first_name / last_name / first_name_kanji / last_name_kanji /
        first_name_kana / last_name_kana / date_of_birth /
        billing_postal_code / billing_state / billing_city /
        billing_line1 / card_number / card_expiry / card_cvv /
        email / password / phone (留给调用方填接码号)
    """
    base = generate_jp_identity(gender=gender, card_brand=card_brand)
    # 跟 pipeline._jp_payload + aBaiAutoplus identity dict 字段名对齐
    return {
        # 漢字（PayPal #firstName / #lastName）
        "first_name_kanji": base["first_name_kanji"],
        "last_name_kanji": base["last_name_kanji"],
        # 片假名（PayPal #countrySpecificFirstName / #countrySpecificLastName）
        "first_name_kana": base["first_name_kana"],
        "last_name_kana": base["last_name_kana"],
        # 通用名（漢字版）
        "first_name": base["first_name_kanji"],
        "last_name": base["last_name_kanji"],
        "name": base["full_name_kanji"],
        # 罗马字版（生成邮箱、卡上印的 latin 化用）
        "first_name_romaji": base["first_name_romaji"],
        "last_name_romaji": base["last_name_romaji"],
        # 出生日期
        "date_of_birth": base["date_of_birth"],
        # 地址
        "billing_postal_code": base["postal_code"],
        "billing_state": base["prefecture"],
        "billing_city": base["city"],
        "billing_line1": base["street"],
        "billing_country": "JP",
        # 兼容 card_pool 的 address dict
        "address": {
            "street": base["street"],
            "city": base["city"],
            "state": base["prefecture"],
            "zip": base["postal_code"],
            "country": "JP",
        },
        # 卡（无空格）
        "card_number": base["card_number"],
        "card_number_formatted": base["card_number_formatted"],
        "cardNumber": base["card_number"],
        "card_bin": base["card_bin"],
        "card_brand": base["card_brand"],
        "card_expiry": base["card_expiry"],
        "cardExpiry": base["card_expiry"],
        "card_cvv": base["card_cvv"],
        "cardCvv": base["card_cvv"],
        # PayPal 账号
        "email": base["email"],
        "password": base["password"],
        # 元
        "gender": base["gender"],
        "region": "JP",
        # 兼容 pipeline._jp_payload 老字段（直接传给 _react_force_fill）
        "first_kanji": base["first_name_kanji"],
        "last_kanji": base["last_name_kanji"],
        "first_kana": base["first_name_kana"],
        "last_kana": base["last_name_kana"],
    }


__all__ = [
    "JP_LAST_NAMES",
    "JP_FIRST_NAMES",
    "JP_ADDRESSES",
    "JP_CARD_BINS",
    "generate_jp_identity",
    "generate_jp_identity_for_paypal",
]


if __name__ == "__main__":
    # CLI: python3 jp_identity.py
    import json as _json
    data = generate_jp_identity()
    print(_json.dumps(data, ensure_ascii=False, indent=2))
