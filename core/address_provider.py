"""
账单地址 provider —— 支持多国（默认 US，可切 JP）

参考 GuJumpgate/FlowPilot/background/steps/fill-plus-checkout.js 里的
MEIGUODIZHI_COUNTRY_CONFIG，从 https://www.meiguodizhi.com/api/v1/dz 拉取
对应国家的真实地址。失败时回退到本地 seed。

调用：
    from address_provider import get_billing_address
    addr = get_billing_address("JP")            # 自动拉 meiguodizhi /jp-address
    addr = get_billing_address("US", base=card_addr)  # US 仍用卡里带的真实地址
"""
from __future__ import annotations

import json
import logging
import random
import ssl
import urllib.error
import urllib.request

from gb_identity import GB_ADDRESSES

log = logging.getLogger("address_provider")

# 跟 GuJumpgate/MEIGUODIZHI_COUNTRY_CONFIG 对齐
COUNTRY_CONFIG = {
    "AR": {"path": "/ar-address", "city": "Buenos Aires"},
    "AU": {"path": "/au-address", "city": "Sydney"},
    "BR": {"path": "/br-address", "city": "São Paulo"},
    "CA": {"path": "/ca-address", "city": "Toronto"},
    "CN": {"path": "/cn-address", "city": "Shanghai"},
    "DE": {"path": "/de-address", "city": "Berlin"},
    "ES": {"path": "/es-address", "city": "Madrid"},
    "FR": {"path": "/fr-address", "city": "Paris"},
    "GB": {"path": "/uk-address", "city": "London"},
    "HK": {"path": "/hk-address", "city": "Hong Kong"},
    "ID": {"path": "/id-address", "city": "Jakarta"},
    "IT": {"path": "/it-address", "city": "Rome"},
    "JP": {"path": "/jp-address", "city": "Tokyo"},
    "KR": {"path": "/kr-address", "city": "Seoul"},
    "MY": {"path": "/my-address", "city": "Kuala Lumpur"},
    "NL": {"path": "/nl-address", "city": "Amsterdam"},
    "PH": {"path": "/ph-address", "city": "Manila"},
    "RU": {"path": "/ru-address", "city": "Moscow"},
    "SG": {"path": "/sg-address", "city": "Singapore"},
    "TH": {"path": "/th-address", "city": "Bangkok"},
    "TR": {"path": "/tr-address", "city": "Istanbul"},
    "TW": {"path": "/tw-address", "city": "Taipei"},
    "US": {"path": "/", "city": "New York"},
    "VN": {"path": "/vn-address", "city": "Ho Chi Minh City"},
}

# 别名 → ISO code（参照 GuJumpgate COUNTRY_ALIASES）
COUNTRY_ALIASES = {
    "JP": ["jp", "jpn", "japan", "日本", "日本国"],
    "US": ["us", "usa", "united states", "united states of america", "america", "美国"],
    "DE": ["de", "deu", "germany", "deutschland", "德国"],
    "FR": ["fr", "fra", "france", "法国"],
    "GB": ["gb", "uk", "united kingdom", "britain", "england", "英国"],
    "ID": ["id", "indonesia", "印度尼西亚", "印尼"],
    "KR": ["kr", "kor", "korea", "south korea", "韩国"],
    "AU": ["au", "aus", "australia", "澳大利亚"],
    "BR": ["br", "bra", "brazil", "brasil", "巴西"],
}

# 各国本地 fallback seed（meiguodizhi 不可用时用）
LOCAL_SEEDS = {
    "JP": [
        # 日文真实地址池（覆盖多个都道府県，每次随机抽一个）
        {"street": "丸の内1-9-2", "city": "千代田区", "state": "東京都", "zip": "100-0005", "country": "JP"},
        {"street": "西新宿2-8-1", "city": "新宿区", "state": "東京都", "zip": "163-8001", "country": "JP"},
        {"street": "六本木6-10-1", "city": "港区", "state": "東京都", "zip": "106-6108", "country": "JP"},
        {"street": "梅田3-1-3", "city": "大阪市北区", "state": "大阪府", "zip": "530-0001", "country": "JP"},
        {"street": "難波5-1-60", "city": "大阪市中央区", "state": "大阪府", "zip": "542-0076", "country": "JP"},
        {"street": "心斎橋筋2-6-14", "city": "大阪市中央区", "state": "大阪府", "zip": "542-0085", "country": "JP"},
        {"street": "博多駅前2-1-1", "city": "福岡市博多区", "state": "福岡県", "zip": "812-0011", "country": "JP"},
        {"street": "天神1-8-1", "city": "福岡市中央区", "state": "福岡県", "zip": "810-0001", "country": "JP"},
        {"street": "栄3-6-1", "city": "名古屋市中区", "state": "愛知県", "zip": "460-0008", "country": "JP"},
        {"street": "名駅1-1-4", "city": "名古屋市中村区", "state": "愛知県", "zip": "450-0002", "country": "JP"},
        {"street": "三宮町1-9-1", "city": "神戸市中央区", "state": "兵庫県", "zip": "650-0021", "country": "JP"},
        {"street": "下京区四条通烏丸東入", "city": "京都市下京区", "state": "京都府", "zip": "600-8006", "country": "JP"},
        {"street": "中区本町4-1-13", "city": "横浜市中区", "state": "神奈川県", "zip": "231-0005", "country": "JP"},
        {"street": "大宮区桜木町1-7-5", "city": "さいたま市大宮区", "state": "埼玉県", "zip": "330-0854", "country": "JP"},
        {"street": "中央区富士見2-3-1", "city": "千葉市中央区", "state": "千葉県", "zip": "260-0015", "country": "JP"},
        {"street": "青葉区国分町3-6-1", "city": "仙台市青葉区", "state": "宮城県", "zip": "980-0803", "country": "JP"},
        {"street": "中央区北5条西2-5", "city": "札幌市中央区", "state": "北海道", "zip": "060-0005", "country": "JP"},
        {"street": "中区大手町3-7-47", "city": "広島市中区", "state": "広島県", "zip": "730-0051", "country": "JP"},
        {"street": "北区丸の内2-20-1", "city": "さいたま市北区", "state": "埼玉県", "zip": "331-0802", "country": "JP"},
        {"street": "高松町26-33", "city": "高松市", "state": "香川県", "zip": "760-0011", "country": "JP"},
    ],
    "US": [
        {"street": "Broadway", "city": "New York",
         "state": "NY", "zip": "10007", "country": "US"},
    ],
    "GB": [dict(address) for address in GB_ADDRESSES],
    "BR": [
        {"street": "Avenida Paulista, 1000", "city": "São Paulo", "state": "SP", "zip": "01310-100", "country": "BR"},
        {"street": "Rua Augusta, 1500", "city": "São Paulo", "state": "SP", "zip": "01305-100", "country": "BR"},
        {"street": "Avenida Atlântica, 1702", "city": "Rio de Janeiro", "state": "RJ", "zip": "22021-001", "country": "BR"},
        {"street": "Rua Visconde de Pirajá, 303", "city": "Rio de Janeiro", "state": "RJ", "zip": "22410-001", "country": "BR"},
        {"street": "Avenida Afonso Pena, 867", "city": "Belo Horizonte", "state": "MG", "zip": "30130-003", "country": "BR"},
        {"street": "Rua XV de Novembro, 621", "city": "Curitiba", "state": "PR", "zip": "80020-310", "country": "BR"},
        {"street": "Rua dos Andradas, 1234", "city": "Porto Alegre", "state": "RS", "zip": "90020-007", "country": "BR"},
        {"street": "SCS Quadra 2, Bloco C", "city": "Brasília", "state": "DF", "zip": "70302-000", "country": "BR"},
    ],
}

# 47 都道府県 EN ↔ JA（hosted checkout JP 模式 region 选择用）
JP_PREFECTURES = [
    ("Hokkaido", "北海道"), ("Aomori", "青森県"), ("Iwate", "岩手県"),
    ("Miyagi", "宮城県"), ("Akita", "秋田県"), ("Yamagata", "山形県"),
    ("Fukushima", "福島県"), ("Ibaraki", "茨城県"), ("Tochigi", "栃木県"),
    ("Gunma", "群馬県"), ("Saitama", "埼玉県"), ("Chiba", "千葉県"),
    ("Tokyo", "東京都"), ("Kanagawa", "神奈川県"), ("Niigata", "新潟県"),
    ("Toyama", "富山県"), ("Ishikawa", "石川県"), ("Fukui", "福井県"),
    ("Yamanashi", "山梨県"), ("Nagano", "長野県"), ("Gifu", "岐阜県"),
    ("Shizuoka", "静岡県"), ("Aichi", "愛知県"), ("Mie", "三重県"),
    ("Shiga", "滋賀県"), ("Kyoto", "京都府"), ("Osaka", "大阪府"),
    ("Hyogo", "兵庫県"), ("Nara", "奈良県"), ("Wakayama", "和歌山県"),
    ("Tottori", "鳥取県"), ("Shimane", "島根県"), ("Okayama", "岡山県"),
    ("Hiroshima", "広島県"), ("Yamaguchi", "山口県"), ("Tokushima", "徳島県"),
    ("Kagawa", "香川県"), ("Ehime", "愛媛県"), ("Kochi", "高知県"),
    ("Fukuoka", "福岡県"), ("Saga", "佐賀県"), ("Nagasaki", "長崎県"),
    ("Kumamoto", "熊本県"), ("Oita", "大分県"), ("Miyazaki", "宮崎県"),
    ("Kagoshima", "鹿児島県"), ("Okinawa", "沖縄県"),
]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

ENDPOINT = "https://www.meiguodizhi.com/api/v1/dz"


def normalize_country_code(value: str) -> str:
    """'日本' / 'jp' / 'JPN' → 'JP'。识别不出返回大写 trim 原值。"""
    if not value:
        return ""
    raw = str(value).strip().lower()
    upper = raw.upper()
    if upper in COUNTRY_CONFIG:
        return upper
    for code, aliases in COUNTRY_ALIASES.items():
        if raw in aliases or any(raw == a or raw == a.lower() for a in aliases):
            return code
    return upper


def _http_post_json(url: str, payload: dict, timeout: float = 15) -> dict:
    """轻量 POST JSON 请求（只用 stdlib，避免再加 requests 依赖）。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.meiguodizhi.com",
            "Referer": "https://www.meiguodizhi.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _normalize_jp_postal(zip_code: str) -> str:
    """JP 邮编标准化：'1000005' → '100-0005'，已经带横杠就保持原样。"""
    s = str(zip_code or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 7:
        return f"{digits[:3]}-{digits[3:]}"
    return s


def _normalize_br_postal(zip_code: str) -> str:
    """BR CEP 标准化：'01310100' → '01310-100'。"""
    s = str(zip_code or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:5]}-{digits[5:]}"
    return s


def _normalize_gb_region(region: str, city: str = "") -> str:
    """Map broad UK regions to County values used by PayPal billingState."""
    raw = str(region or "").strip()
    city_key = str(city or "").strip().lower()
    city_regions = {
        "london": "London",
        "manchester": "Greater Manchester",
        "birmingham": "West Midlands",
        "liverpool": "Merseyside",
        "bristol": "Avon",
        "edinburgh": "Edinburgh City",
        "glasgow": "Glasgow City",
        "cardiff": "Cardiff",
        "belfast": "Belfast City",
    }
    if city_key in city_regions:
        return city_regions[city_key]
    # PayPal's UK selector does not contain the broad value "England".
    return "London" if raw.lower() in {"", "england", "united kingdom", "uk", "gb"} else raw


def _normalize_jp_region(region: str) -> str:
    """meiguodizhi /jp-address 返回的 State 可能是 '東京都' / 'Tokyo' / 'Tokyo-to' /
    'Tokyo Prefecture' 等多种形态。统一映射成 hosted checkout JP option 的
    英文 prefecture name（'Tokyo'）。"""
    if not region:
        return ""
    s = str(region).strip()
    # 先尝试英文匹配
    for en, ja in JP_PREFECTURES:
        if s.lower() == en.lower():
            return en
    # 日文匹配
    for en, ja in JP_PREFECTURES:
        if s == ja:
            return en
    # 模糊匹配（'Tokyo-to' 'Tokyo Prefecture' 'Tokyo Metropolis'）
    low = s.lower().replace("-to", "").replace("-fu", "").replace("-ken", "").strip()
    low = low.replace("prefecture", "").replace("metropolis", "").strip()
    for en, ja in JP_PREFECTURES:
        if low == en.lower():
            return en
    return s  # 兜底原样


def _meiguodizhi_address(country_code: str, city_hint: str = "") -> dict | None:
    """调 https://www.meiguodizhi.com/api/v1/dz 拿一条对应国家的真实地址。
    失败返回 None。"""
    cfg = COUNTRY_CONFIG.get(country_code)
    if not cfg:
        return None
    payload = {
        "city": city_hint or cfg["city"],
        "path": cfg["path"],
        "method": "refresh",
    }
    try:
        data = _http_post_json(ENDPOINT, payload)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.warning(f"  [addr] meiguodizhi {country_code} 网络错误: {e}")
        return None
    except (ValueError, json.JSONDecodeError) as e:
        log.warning(f"  [addr] meiguodizhi {country_code} 解析失败: {e}")
        return None

    if not isinstance(data, dict) or data.get("status") != "ok":
        log.warning(f"  [addr] meiguodizhi {country_code} 状态异常: {str(data)[:120]}")
        return None

    a = data.get("address") if isinstance(data.get("address"), dict) else {}
    if not a:
        return None

    # 不同国家 schema 略有差异：JP 用 Trans_Address (英文版本)，US 用 Address。
    line1 = (a.get("Trans_Address") or a.get("Address") or "").strip()
    city = (a.get("City") or "").strip()
    state_raw = (a.get("State_Full") or a.get("State") or "").strip()
    zip_code = (a.get("Zip_Code") or "").strip()

    if not line1 or not city or not zip_code:
        log.warning(f"  [addr] meiguodizhi {country_code} 字段不全: line1={line1!r} city={city!r} zip={zip_code!r}")
        return None

    addr = {
        "street": line1,
        "city": city,
        "state": state_raw,
        "zip": zip_code,
        "country": country_code,
        "_source": "meiguodizhi",
    }

    # JP 特化：邮编加横杠 + 都道府県英文规范化
    if country_code == "JP":
        addr["zip"] = _normalize_jp_postal(addr["zip"])
        addr["state"] = _normalize_jp_region(addr["state"]) or "Tokyo"
    elif country_code == "BR":
        addr["zip"] = _normalize_br_postal(addr["zip"])
        addr["state"] = str(addr.get("state") or "").upper()
    elif country_code == "GB":
        addr["state"] = _normalize_gb_region(addr.get("state", ""), addr.get("city", ""))

    return addr


def get_billing_address(country: str, base: dict | None = None,
                        prefer_remote: bool = True) -> dict:
    """拿一条 country 对应的账单地址。

    Args:
        country: ISO code 或别名（'JP' / 'Japan' / '日本' / 'us' 都接受）
        base: 已有地址（比如 cards.txt 里卡自带的 US 地址）。
              如果 country == base['country'] 就直接返回 base，不走远程。
        prefer_remote: True = 先试 meiguodizhi，失败回退本地 seed；
                       False = 只用本地 seed。

    Returns:
        dict: {street, city, state, zip, country, _source}
    """
    code = normalize_country_code(country) or "US"

    # 用户已经提供了同国地址（典型场景：US 卡带的就是真实 US 地址），直接返回
    if base and isinstance(base, dict):
        base_country = normalize_country_code(base.get("country") or "US")
        if base_country == code and base.get("street") and base.get("city"):
            log.info(f"  [addr] 用 base {code} 地址: {base.get('city')} {base.get('zip')}")
            normalized = {**base, "country": code, "_source": "base"}
            if code == "GB":
                normalized["state"] = _normalize_gb_region(
                    normalized.get("state", ""), normalized.get("city", "")
                )
            return normalized

    # JP and GB use grouped local data. The remote GB generator has returned
    # plausible-looking but non-existent postcode districts (for example EC7B),
    # which PayPal rejects even though the loose postcode shape looks valid.
    if prefer_remote and code in COUNTRY_CONFIG and code not in {"JP", "GB"}:
        addr = _meiguodizhi_address(code)
        if addr:
            log.info(f"  [addr] meiguodizhi {code}: {addr['city']} / {addr['zip']} / {addr['state']}")
            return addr

    # 本地 seed 兜底
    seeds = LOCAL_SEEDS.get(code) or []
    if seeds:
        seed = random.choice(seeds)
        log.info(f"  [addr] 用本地 seed {code}: {seed['city']} / {seed['zip']}")
        return {**seed, "_source": "local_seed"}

    # 全失败 → 用 base 兜底（即便国家不对）
    if base:
        log.warning(f"  [addr] {code} 无 seed 也无远程，回退到 base")
        return {**base, "_source": "base_fallback"}

    log.error(f"  [addr] {code} 完全没有可用地址")
    return {"street": "", "city": "", "state": "", "zip": "",
            "country": code, "_source": "empty"}


def jp_prefecture_aliases(name: str) -> list[str]:
    """给定 'Tokyo' / '東京都' 之一，返回所有可能的匹配字符串（用于 select option 匹配）。"""
    s = (name or "").strip()
    out = [s]
    for en, ja in JP_PREFECTURES:
        if s.lower() == en.lower() or s == ja:
            out.extend([en, ja, en + "-to", en + "-fu", en + "-ken",
                        en + " Prefecture", en + " Metropolis"])
            break
    # 去重保序
    seen = set()
    uniq = []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq
