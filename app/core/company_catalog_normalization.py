"""Stable display normalization for Company Finder records and facets."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


MISSING_LABEL = "未提供"

SIZE_LABELS = {
    "1-10": "1–10 人",
    "11-50": "11–50 人",
    "51-200": "51–200 人",
    "201-500": "201–500 人",
    "501-1000": "501–1,000 人",
    "1001-5000": "1,001–5,000 人",
    "5001-10000": "5,001–10,000 人",
    "10001+": "10,001+ 人",
}

# The source catalogue uses LinkedIn's legacy 147-industry taxonomy. Keeping
# the source value as the stable filter key prevents localized labels from
# changing query semantics.
INDUSTRY_LABELS = {
    "accounting": "会计服务",
    "airlines/aviation": "航空运输",
    "alternative dispute resolution": "替代性争议解决",
    "alternative medicine": "替代医疗",
    "animation": "动画制作",
    "apparel & fashion": "服装与时尚",
    "architecture & planning": "建筑与规划",
    "arts and crafts": "艺术与工艺",
    "automotive": "汽车",
    "aviation & aerospace": "航空航天",
    "banking": "银行",
    "biotechnology": "生物技术",
    "broadcast media": "广播媒体",
    "building materials": "建筑材料",
    "business supplies and equipment": "商业用品与设备",
    "capital markets": "资本市场",
    "chemicals": "化工",
    "civic & social organization": "公民与社会组织",
    "civil engineering": "土木工程",
    "commercial real estate": "商业地产",
    "computer & network security": "计算机与网络安全",
    "computer games": "电子游戏",
    "computer hardware": "计算机硬件",
    "computer networking": "计算机网络",
    "computer software": "计算机软件",
    "construction": "建筑施工",
    "consumer electronics": "消费电子",
    "consumer goods": "消费品",
    "consumer services": "消费者服务",
    "cosmetics": "化妆品",
    "dairy": "乳制品",
    "defense & space": "国防与航天",
    "design": "设计",
    "e-learning": "在线教育",
    "education management": "教育管理",
    "electrical/electronic manufacturing": "电气与电子制造",
    "entertainment": "娱乐",
    "environmental services": "环境服务",
    "events services": "会展与活动服务",
    "executive office": "行政办公室",
    "facilities services": "设施管理服务",
    "farming": "农业",
    "financial services": "金融服务",
    "fine art": "美术",
    "fishery": "渔业",
    "food & beverages": "食品与饮料",
    "food production": "食品生产",
    "fund-raising": "募资",
    "furniture": "家具",
    "gambling & casinos": "博彩与娱乐场",
    "glass, ceramics & concrete": "玻璃、陶瓷与混凝土",
    "government administration": "政府行政",
    "government relations": "政府关系",
    "graphic design": "平面设计",
    "health, wellness and fitness": "健康、养生与健身",
    "higher education": "高等教育",
    "hospital & health care": "医院与医疗保健",
    "hospitality": "酒店与接待服务",
    "human resources": "人力资源",
    "import and export": "进出口",
    "individual & family services": "个人与家庭服务",
    "industrial automation": "工业自动化",
    "information services": "信息服务",
    "information technology and services": "信息技术与服务",
    "insurance": "保险",
    "international affairs": "国际事务",
    "international trade and development": "国际贸易与发展",
    "internet": "互联网",
    "investment banking": "投资银行",
    "investment management": "投资管理",
    "judiciary": "司法机构",
    "law enforcement": "执法机构",
    "law practice": "律师事务所",
    "legal services": "法律服务",
    "legislative office": "立法机构",
    "leisure, travel & tourism": "休闲、旅行与旅游",
    "libraries": "图书馆",
    "logistics and supply chain": "物流与供应链",
    "luxury goods & jewelry": "奢侈品与珠宝",
    "machinery": "机械设备",
    "management consulting": "管理咨询",
    "maritime": "海事",
    "market research": "市场研究",
    "marketing and advertising": "市场营销与广告",
    "mechanical or industrial engineering": "机械与工业工程",
    "media production": "媒体制作",
    "medical devices": "医疗器械",
    "medical practice": "医疗机构",
    "mental health care": "心理健康服务",
    "military": "军事",
    "mining & metals": "矿业与金属",
    "motion pictures and film": "电影制作",
    "museums and institutions": "博物馆与文化机构",
    "music": "音乐",
    "nanotechnology": "纳米技术",
    "newspapers": "报纸",
    "non-profit organization management": "非营利组织管理",
    "oil & energy": "石油与能源",
    "online media": "在线媒体",
    "outsourcing/offshoring": "外包与离岸服务",
    "package/freight delivery": "包裹与货运配送",
    "packaging and containers": "包装与容器",
    "paper & forest products": "纸业与林产品",
    "performing arts": "表演艺术",
    "pharmaceuticals": "制药",
    "philanthropy": "公益慈善",
    "photography": "摄影",
    "plastics": "塑料",
    "political organization": "政治组织",
    "primary/secondary education": "中小学教育",
    "printing": "印刷",
    "professional training & coaching": "职业培训与教练服务",
    "program development": "项目与计划开发",
    "public policy": "公共政策",
    "public relations and communications": "公关与传播",
    "public safety": "公共安全",
    "publishing": "出版",
    "railroad manufacture": "铁路设备制造",
    "ranching": "牧场经营",
    "real estate": "房地产",
    "recreational facilities and services": "休闲设施与服务",
    "religious institutions": "宗教机构",
    "renewables & environment": "可再生能源与环境",
    "research": "科学研究",
    "restaurants": "餐饮",
    "retail": "零售",
    "security and investigations": "安保与调查",
    "semiconductors": "半导体",
    "shipbuilding": "船舶制造",
    "sporting goods": "体育用品",
    "sports": "体育",
    "staffing and recruiting": "人才派遣与招聘",
    "supermarkets": "超市",
    "telecommunications": "电信通信",
    "textiles": "纺织",
    "think tanks": "智库",
    "tobacco": "烟草",
    "translation and localization": "翻译与本地化",
    "transportation/trucking/railroad": "交通运输、公路与铁路",
    "utilities": "公用事业",
    "venture capital & private equity": "风险投资与私募股权",
    "veterinary": "兽医服务",
    "warehousing": "仓储",
    "wholesale": "批发",
    "wine and spirits": "葡萄酒与烈酒",
    "wireless": "无线通信",
    "writing and editing": "写作与编辑",
}

_COUNTRY_OVERRIDES = {
    "cape verde": "佛得角",
    "democratic republic of the congo": "刚果民主共和国",
    "micronesia": "密克罗尼西亚联邦",
    "palestine": "巴勒斯坦",
    "republic of the congo": "刚果共和国",
    "the gambia": "冈比亚",
    "vatican city": "梵蒂冈",
}

_REGION_LABELS = {
    "australia": {
        "new south wales": "新南威尔士州", "victoria": "维多利亚州", "queensland": "昆士兰州",
        "western australia": "西澳大利亚州", "south australia": "南澳大利亚州", "tasmania": "塔斯马尼亚州",
        "australian capital territory": "澳大利亚首都领地", "northern territory": "北领地",
    },
    "brazil": {
        "são paulo": "圣保罗州", "minas gerais": "米纳斯吉拉斯州", "rio de janeiro": "里约热内卢州",
        "rio grande do sul": "南里奥格兰德州", "paraná": "巴拉那州", "santa catarina": "圣卡塔琳娜州",
        "bahia": "巴伊亚州", "goiás": "戈亚斯州", "pernambuco": "伯南布哥州", "ceará": "塞阿拉州",
        "espírito santo": "圣埃斯皮里图州", "federal district": "联邦区", "mato grosso do sul": "南马托格罗索州",
        "mato grosso": "马托格罗索州", "pará": "帕拉州",
    },
    "canada": {
        "ontario": "安大略省", "quebec": "魁北克省", "british columbia": "不列颠哥伦比亚省",
        "alberta": "阿尔伯塔省", "manitoba": "马尼托巴省", "nova scotia": "新斯科舍省",
        "saskatchewan": "萨斯喀彻温省", "new brunswick": "新不伦瑞克省", "newfoundland and labrador": "纽芬兰与拉布拉多省",
        "prince edward island": "爱德华王子岛省", "yukon": "育空地区", "northwest territory": "西北地区",
        "northwest territories": "西北地区", "nunavut": "努纳武特地区",
    },
    "china": {
        "guangdong": "广东省", "jiangsu": "江苏省", "zhejiang": "浙江省", "shandong": "山东省", "shanghai": "上海市",
        "beijing": "北京市", "henan": "河南省", "fujian": "福建省", "sichuan": "四川省", "hebei": "河北省",
        "shaanxi": "陕西省", "hubei": "湖北省", "anhui": "安徽省", "hunan": "湖南省", "liaoning": "辽宁省",
    },
    "france": {
        "île-de-france": "法兰西岛大区", "auvergne-rhône-alpes": "奥弗涅-罗纳-阿尔卑斯大区",
        "hauts-de-france": "上法兰西大区", "occitanie": "奥克西塔尼大区", "nouvelle-aquitaine": "新阿基坦大区",
        "provence-alpes-côte d'azur": "普罗旺斯-阿尔卑斯-蓝色海岸大区", "grand est": "大东部大区",
        "pays de la loire": "卢瓦尔河地区大区", "bretagne": "布列塔尼大区", "normandie": "诺曼底大区",
        "centre-val de loire": "中央-卢瓦尔河谷大区", "bourgogne-franche-comté": "勃艮第-弗朗什-孔泰大区",
        "corse": "科西嘉大区", "centre": "中央-卢瓦尔河谷大区",
    },
    "germany": {
        "north rhine-westphalia": "北莱茵-威斯特法伦州", "bavaria": "巴伐利亚州", "baden-württemberg": "巴登-符腾堡州",
        "berlin": "柏林州", "hesse": "黑森州", "lower saxony": "下萨克森州", "hamburg": "汉堡州",
        "rhineland-palatinate": "莱茵兰-普法尔茨州", "saxony": "萨克森州", "schleswig-holstein": "石勒苏益格-荷尔斯泰因州",
        "brandenburg": "勃兰登堡州", "thuringia": "图林根州", "saxony-anhalt": "萨克森-安哈尔特州",
        "mecklenburg-vorpommern": "梅克伦堡-前波美拉尼亚州", "saarland": "萨尔州",
    },
    "india": {
        "maharashtra": "马哈拉施特拉邦", "delhi": "德里", "karnataka": "卡纳塔克邦", "tamil nadu": "泰米尔纳德邦",
        "uttar pradesh": "北方邦", "gujarat": "古吉拉特邦", "telangana": "特伦甘纳邦", "west bengal": "西孟加拉邦",
        "haryana": "哈里亚纳邦", "rajasthan": "拉贾斯坦邦", "kerala": "喀拉拉邦", "madhya pradesh": "中央邦",
        "punjab": "旁遮普邦", "andhra pradesh": "安得拉邦", "bihar": "比哈尔邦",
    },
    "italy": {
        "lombardy": "伦巴第大区", "veneto": "威尼托大区", "lazio": "拉齐奥大区", "emilia-romagna": "艾米利亚-罗马涅大区",
        "tuscany": "托斯卡纳大区", "piemonte": "皮埃蒙特大区", "campania": "坎帕尼亚大区", "sicily": "西西里大区",
        "apulia": "普利亚大区", "liguria": "利古里亚大区", "marche": "马尔凯大区", "trentino-south tyrol": "特伦蒂诺-上阿德杰大区",
        "friuli-venezia giulia": "弗留利-威尼斯朱利亚大区", "abruzzo": "阿布鲁佐大区", "sardinia": "撒丁大区",
    },
    "netherlands": {
        "north holland": "北荷兰省", "south holland": "南荷兰省", "north brabant": "北布拉班特省", "gelderland": "海德兰省",
        "utrecht": "乌得勒支省", "overijssel": "上艾塞尔省", "limburg": "林堡省", "friesland": "弗里斯兰省",
        "groningen": "格罗宁根省", "drenthe": "德伦特省", "flevoland": "弗莱沃兰省", "zeeland": "泽兰省",
    },
    "spain": {
        "community of madrid": "马德里自治区", "catalonia": "加泰罗尼亚自治区", "andalusia": "安达卢西亚自治区",
        "valencian community": "瓦伦西亚自治区", "canary islands": "加那利群岛自治区", "galicia": "加利西亚自治区",
        "basque country": "巴斯克自治区", "castilla and león": "卡斯蒂利亚-莱昂自治区", "castilla-la mancha": "卡斯蒂利亚-拉曼恰自治区",
        "balearic islands": "巴利阿里群岛自治区", "region of murcia": "穆尔西亚自治区", "aragon": "阿拉贡自治区",
        "asturias": "阿斯图里亚斯自治区", "extremadura": "埃斯特雷马杜拉自治区", "navarre": "纳瓦拉自治区",
    },
    "united kingdom": {
        "england": "英格兰", "scotland": "苏格兰", "wales": "威尔士", "northern ireland": "北爱尔兰",
    },
    "united states": {
        "california": "加利福尼亚州", "texas": "得克萨斯州", "florida": "佛罗里达州", "new york": "纽约州",
        "illinois": "伊利诺伊州", "pennsylvania": "宾夕法尼亚州", "georgia": "佐治亚州", "ohio": "俄亥俄州",
        "new jersey": "新泽西州", "north carolina": "北卡罗来纳州", "michigan": "密歇根州", "washington": "华盛顿州",
        "massachusetts": "马萨诸塞州", "colorado": "科罗拉多州", "virginia": "弗吉尼亚州",
    },
}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_COMPANY_EDGE_RE = re.compile(r"^[!|¡]+\s*|\s*[!|¡]+$")
_QUOTED_PREFIX_RE = re.compile(r"^[\"'](\([^)]{1,80}\))[\"']\s*")
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_COMPANY_WORD_CASE = {
    "Ab": "AB", "Ag": "AG", "Ai": "AI", "As": "AS", "Bv": "B.V.",
    "Gmbh": "GmbH", "Inc": "Inc", "It": "IT", "Llc": "LLC", "Llp": "LLP",
    "Ltd": "Ltd", "Nv": "N.V.", "Oy": "Oy", "Plc": "PLC", "Pte": "Pte",
    "Pty": "Pty", "Sa": "S.A.", "Sas": "SAS", "Spa": "S.p.A.",
    "Srl": "S.r.l.", "Uk": "UK", "Us": "US", "Usa": "USA",
}
_COMPANY_SMALL_WORDS = {"And", "At", "By", "Da", "De", "Del", "Do", "Dos", "Et", "For", "In", "La", "Le", "Of", "On", "The", "To", "Y"}


def _text(value: object) -> str:
    raw = html.unescape(str(value or ""))
    normalized = unicodedata.normalize("NFKC", raw).replace("\u00a0", " ")
    normalized = _CONTROL_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _lookup_key(value: object) -> str:
    return _text(value).lower().replace("’", "'").replace("‘", "'")


@lru_cache(maxsize=1)
def _country_labels() -> dict[str, str]:
    path = Path(__file__).with_name("company_catalog_countries.json")
    labels = json.loads(path.read_text(encoding="utf-8"))
    return {_lookup_key(key): str(value) for key, value in labels.items()}


def _smart_title(value: str) -> str:
    if not value or not any(character.isalpha() for character in value):
        return value
    if value != value.lower() and value != value.upper():
        return value
    words = value.title().split(" ")
    for index, word in enumerate(words):
        bare = word.strip(".,()[]{}")
        if bare in _COMPANY_WORD_CASE:
            words[index] = word.replace(bare, _COMPANY_WORD_CASE[bare])
        elif index and bare in _COMPANY_SMALL_WORDS:
            words[index] = word.replace(bare, bare.lower())
    return " ".join(words)


def normalize_company_name(value: object) -> str:
    name = _text(value)
    previous = None
    while name and name != previous:
        previous = name
        name = _COMPANY_EDGE_RE.sub("", name).strip()
    name = _QUOTED_PREFIX_RE.sub(r"\1 ", name)
    name = _SPACE_RE.sub(" ", name).strip(" \t\r\n\"'“”‘’")
    return _smart_title(name)


def humanize_catalog_value(value: object) -> str:
    return _smart_title(_text(value).replace("_", " "))


def country_label(value: object) -> str:
    key = _lookup_key(value)
    if not key:
        return ""
    labels = _country_labels()
    return _COUNTRY_OVERRIDES.get(key) or labels.get(key) or labels.get(key.replace(" and ", " & ")) or humanize_catalog_value(value)


def industry_label(value: object) -> str:
    key = _lookup_key(value)
    return INDUSTRY_LABELS.get(key, humanize_catalog_value(value) if key else "")


def region_label(country: object, value: object) -> str:
    country_key = _lookup_key(country)
    region_key = _lookup_key(value)
    if not region_key:
        return ""
    return _REGION_LABELS.get(country_key, {}).get(region_key) or humanize_catalog_value(value)


def size_label(value: object) -> str:
    key = _lookup_key(value).replace("–", "-").replace("—", "-").replace(" ", "")
    return SIZE_LABELS.get(key, humanize_catalog_value(value) if key else "")


def _hostname(hostname: str) -> str:
    value = hostname.strip().strip(".").lower()
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if "." not in ascii_value or any(not part for part in ascii_value.split(".")):
        return ""
    return ascii_value


def normalize_website(value: object) -> tuple[str, str]:
    raw = _text(value)
    if not raw:
        return "", ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(candidate)
        host = _hostname(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        return "", ""
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        return "", ""
    netloc = f"{host}:{port}" if port else host
    path = quote(parsed.path or "", safe="/%:@-._~!$&'()*+,;=")
    query_pairs = [
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    url = urlunsplit(("https", netloc, path.rstrip("/") if path != "/" else "", urlencode(query_pairs), ""))
    return url, host.removeprefix("www.")


def normalize_linkedin_url(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().strip(".")
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    if not any(path.lower().startswith(prefix) for prefix in ("/company/", "/showcase/", "/school/")):
        return ""
    return urlunsplit(("https", "www.linkedin.com", quote(path, safe="/%:@-._~!$&'()*+,;="), "", ""))


def canonical_filter_value(facet: str, value: object) -> str:
    raw = _lookup_key(value)
    if not raw:
        return ""
    if facet == "country":
        for source, label in _country_labels().items():
            if label == _text(value):
                return source
    elif facet == "industry":
        for source, label in INDUSTRY_LABELS.items():
            if label == _text(value):
                return source
    elif facet == "size":
        for source, label in SIZE_LABELS.items():
            if label == _text(value):
                return source
    return raw


def facet_label(facet: str, value: object) -> str:
    if facet == "country":
        return country_label(value)
    if facet == "industry":
        return industry_label(value)
    if facet == "size":
        return size_label(value)
    return humanize_catalog_value(value)


def normalize_facet_items(facet: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**item, "value": _lookup_key(item.get("value")), "label": facet_label(facet, item.get("value"))}
        for item in items if _lookup_key(item.get("value"))
    ]


def normalize_company_record(record: dict[str, Any]) -> dict[str, Any]:
    item = dict(record)
    website_url, website_domain = normalize_website(item.get("website"))
    region = region_label(item.get("country"), item.get("region"))
    locality = humanize_catalog_value(item.get("locality"))
    location = [country_label(item.get("country")), region, locality]
    item.update({
        "name_display": normalize_company_name(item.get("name")),
        "country_label": location[0],
        "region_label": region,
        "locality_label": locality,
        "location_label": " · ".join(part for part in location if part),
        "industry_label": industry_label(item.get("industry")),
        "size_label": size_label(item.get("size")),
        "website_url": website_url,
        "website_domain": website_domain,
        "linkedin_url": normalize_linkedin_url(item.get("linkedin_url")),
    })
    return item
