"use strict";

/*
 * Word colours: which words of a subtitle have an Anki card, and what that card says about them.
 * Loaded before background.js and content.js (see manifest.json), so both sides read a note the
 * same way:
 *  - the background turns a deck's notes into [word, status, pitch] entries,
 *  - the content script builds an index from them and marks the words of every line.
 *
 * A card's word is a dictionary form; the subtitle has it conjugated (食べる, 食べました). The
 * matcher therefore knows a handful of endings: enough to find a verb or adjective in its usual
 * shapes without a morphological analyser. Beside the deck it colours what needs no card: the
 * words the viewer marked as known and, on request, names and Latin text ("proper") and the
 * particles and the katakana words, both as known. Everything here is pure; there is no DOM.
 */

const SHISUKO_WORDS = (() => {
  // The four a card has, and "proper": a name or Latin text, coloured by rule and never by a deck
  // (mergeStatus() lets any card status win over it).
  const STATUSES = Object.freeze(["new", "learning", "learned", "suspended", "proper"]);
  const PITCHES = Object.freeze(["heiban", "atamadaka", "nakadaka", "odaka"]);
  const LEARNED = "learned";
  const PROPER = "proper";

  // A deck word longer than this is a sentence pasted into the word field, not a word.
  const MAX_WORD_LEN = 40;
  // Particles are never coloured by a card, whatever the deck says: a card for は or のは (Yomitan
  // looks them up like any word) would otherwise paint the は of every line by its own progress.
  // With markWords()' `particles` option they count as known instead, learned wherever they stand:
  // grammar the viewer reads past, never the colour of the word before them (まで after 領域 has no
  // card, and the word's own state is not its). Case, binding, adverbial, conjunctive and
  // sentence-final particles, the fusions Yomitan's dictionaries carry, and the copula and
  // auxiliaries a learner mines as words (です, だ, ます, ない, たい, the contraction ん), which
  // end nearly every line the same way.
  const PARTICLES = new Set([
    "は", "が", "を", "に", "へ", "と", "で", "の", "も", "や", "か", "ね", "よ", "な", "わ", "ぞ", "ぜ", "さ", "し", "て", "ば",
    "から", "まで", "より", "こそ", "さえ", "すら", "しか", "だけ", "ばかり", "ほど", "くらい", "ぐらい", "など", "なんて", "なんか",
    "きり", "っきり", "のみ", "だって", "とか", "って", "ってば", "やら", "だの", "なり", "ずつ", "だに", "ながら", "つつ", "たり",
    "けど", "けども", "けれど", "けれども", "のに", "ので", "のは", "のが", "のを", "のか", "には", "とは", "では", "へは", "とも", "にも",
    "でも", "へも", "への", "との", "での", "かも", "かな", "かしら", "っけ", "よね", "ねえ", "なあ", "かい", "ても", "たら", "なら",
    "だ", "だった", "だろう", "だろ", "です", "でした", "でしょう", "でしょ", "ます", "ません", "ました", "ない", "たい", "ん",
    "じゃ", "じゃん", "んじゃ", "もん",
  ]);
  const PARTICLE_MAX_LEN = Math.max(...[...PARTICLES].map((piece) => piece.length));
  // ICU cuts a kana word it does not know into pieces, and the first of them has a particle's shape
  // as often as not (や|って, な|っ|た, お待ち|し|て, も|ら|って), while the auxiliaries after the
  // stem of a verb the deck lacks are that verb's own inflection (ござい|ます, あっ|て). Measured
  // on 668 of the viewer's subtitle lines with their deck and the real segmenter: of 2,198
  // particles counted as known, 18 in a sample of 200 judged by hand were such a piece. The guard
  // below refused 206, 196 of them no particle; what is left is one in thirty, kana words cut in
  // shapes too rare to list (な|げー, と|ろ, へ|ん) and words the cue boundary cut. The rules
  // added after that sample (the glides, the second kana of a slang word, な|に, the kana a kanji
  // verb is cut into) took 8 more of the 2,026 particles off those lines, and 25 of 2,237 with
  // an empty deck, where no card holds the verb: none of them a particle. It refuses a one-kana
  // particle before っ, the 音便 of the verb it begins (やった, なって, よかった, つながった,
  // かかった; not the copula だ, whose past is だった, nor the nominaliser の of 見るのって, nor
  // ね or よ before a quotative って of its own, 楽しい|ね|って), or before a た or て that ICU
  // left alone or fused with a particle only (猫|が|で|た, 猫|が|で|たよ: 出る, 寝る and 似る,
  // which those lines never had, and which no particle is followed by); a particle before the kana
  // listed here (し of する before て, ま, ち and な, and of しれない and しろ before れ and ろ; も
  // of もらう, alone or ending にも, でも, とも or ても, before ら; か of かかる before か; や of
  // やる before り and れ, of やばい before ば; よ of よい before か; へ of へえ and っけ of でっけえ
  // before え), or before the segments listed, and the second of such a pair (な|に, 何; な|さ and
  // な|さそう, ない's: 申し訳|な|さそう, 情け|な|さ, while 変|な|におい keeps its な); a one-kana
  // particle, or っけ, before a small kana that glides onto the kana before it (おっ|し|ゃ), or
  // before a single kana that is no particle and a small kana after it (だ|せ|ぇ, で|け|ぇ: slang
  // ICU does not know); after a kanji, the kana a verb the deck lacks is cut into: ん, and だ or
  // で after that ん (飲|ん|だ, 死|ん|だ), and the a-row さ, わ, ば, な and か before the
  // negative, the passive or the causative (書|か|ない, 話|さ|ない, 言|わ|ない, 呼|ば|れ|た,
  // 移|さ|れる; not 何|か|ない, where か is the particle; the ん of 僕|ん|家 goes with them);
  // って, the て-form of an う, つ or る verb, after an a-row kana of plain text (な|って, や|って,
  // もら|って, 強ま|って; not とか|って, nor after a word the deck holds, さくら|って), and after
  // ちゃ or じゃ whatever ends there (使っちゃ|って, where the form of a deck verb stops, as the
  // tables do); and the INFLECTIONS below. A longer particle refused, a shorter one may stand (に
  // of にも in 猫|に|も|ら|っ|た). The ten particles among the refused were the Kansai copula や
  // before った and って (日本初やった: the same kana as やる's) and ない after a word the matcher
  // did not find (もちろん|ない, もんじゃ|ない). Not the rule that comes to mind, a particle before
  // a single kana that is no particle: ICU cuts the kana word after a true particle the same way
  // (を|お|ご|ら, に|い|ます, で|ご|ざ|い), and that rule refused 25 particles with its 33 pieces.
  const PARTICLE_NOT_BEFORE = {
    し: ["て", "ま", "ち", "な", "れ", "ろ"],
    も: ["ら"],
    にも: ["ら"],
    でも: ["ら"],
    とも: ["ら"],
    ても: ["ら"],
    か: ["か"],
    や: ["り", "れ", "ば"],
    よ: ["か"],
    へ: ["え"],
    っけ: ["え"],
  };
  // Whole segments, where the kana alone would refuse a true particle before a word (変|な|におい,
  // 好き|な|さかな).
  const PARTICLE_NOT_BEFORE_SEGMENT = { な: ["に", "さ", "さそう"] };
  const A_ROW_BEFORE_TE = /^[あかさなはまやらわがざばぱゃ]$/u;
  // The small kana that glide onto the kana before them: no word begins with one. Not the small
  // vowels, which draw a particle out (だ|よ|ぉ, 猫|が|ぁ).
  const GLIDES = new Set([..."ゃゅょャュョゎヮ"]);
  // The a-row kana of a verb's negative, passive and causative stem after its kanji (書|か|ない),
  // and what follows them there.
  const A_ROW_OKURIGANA = new Set(["さ", "わ", "ば", "な", "か"]);
  const NEGATIVE_HEAD = /^[なずれせ]/u;
  // The particles ICU fuses with the い of いる after them (人|がい|た, 猫|とい|た), and what
  // follows that い when it is いる's. Not は: はいた, はいて and はいる are 履く, 吐く and 入る in
  // kana as often (靴|を|はい|た), and ICU fuses them the same way. Nor と after the 音便 kana of a
  // verb, where とい is ておいて (置い|とい|て, 書い|とい|て).
  const IRU_PARTICLES = new Set(["が", "と", "も", "に", "で"]);
  const ONBIN_KANA = new Set(["い", "っ", "ん"]);
  const IRU_ENDINGS = ["た", "て", "ない", "なかっ", "なく", "ます", "まし", "ませ", "る", "れば", "よう", "たい"];
  // The entries of PARTICLES that are a verb's or adjective's own inflection when they follow the
  // kana of a word the matcher did not find (ござい|ます, やり|たい, あっ|て, 見えてき|ました): the
  // form of a word the deck lacks stays plain as a whole, as the form of a deck word takes its
  // colour as a whole. After a particle, a word, a kanji or punctuation they are taken
  // (お金が|ない, んじゃ|ない, 問題|ない); the okurigana of a kanji verb the deck lacks, cut in a
  // particle's shape of one kana before them (行|か|ない), is refused above, while one of two
  // kana (分|から|ない) is a gap: から after a kanji is the particle as often (朝|から|ない).
  const INFLECTIONS = new Set(["て", "ば", "たら", "たり", "ても", "ながら", "ます", "ません", "ました", "ない", "たい"]);
  // The て or で of a て-form ICU fuses with the く of the auxiliary after it (食|べ|てく|れ|た,
  // 書|い|てく|れ|た, かけ|てく|れ; not 読|んで|く|れ|た or 言|って|く|れ|た, which it cuts), so that
  // くれる never begins a segment there and かけて ends inside one: the index after such a て is
  // a boundary too.
  const TE_FORM = new Set(["て", "で"]);
  // The honorific prefixes ICU cuts off the word they front (お|風呂, ご|家族, お|仕事): the prefix
  // takes the word's colour. It keeps お茶, お前, お金, ご飯 and お母さん whole, and those are words
  // of their own, so 前 in a deck never colours お前.
  const HONORIFICS = new Set(["お", "ご"]);
  // The quotative pieces ICU keeps in one segment with いう (という, っていう, 彼|という|人), so
  // that いう never begins a segment: いう may begin right after one. Longest first. Only いう:
  // ICU cuts って off every other word (って|こと, って|もの) and keeps ところ, とおる, とまる and
  // とくに whole because they are words of their own, so ころ in a deck never colours ところ.
  const QUOTATIVES = ["って", "と"];
  const QUOTED_WORD = "いう";
  // The quotative with いう, one segment to ICU (スタート|という|こと, っていう|の|は,
  // こと|ていう|か): with the particles counted as known it is one of them, a particle combination,
  // where no deck word is found first. という is one when no card holds いう or という (a card for
  // いう colours it as the quotative rule above says, one for という is a word of its own);
  // っていう when none holds いう (the rule above fronts いう with って, and a card for という does
  // not reach into っていう); ていう whatever the deck says, since no quotative fronts いう with て,
  // so a card for いう never shows in こと|ていう|か. Longest first.
  const QUOTE_PHRASES = ["っていう", "ていう", "という"];
  // Latin text is a name or a foreign word to a learner (jr, YouTube, iPhone, TV): a run of letters
  // with the digits, apostrophes, ampersands, dots and hyphens inside it, ending in a letter or
  // digit, and its full-width twin (Ｗｉ－Ｆｉ, Ｑ＆Ａ). Digits alone are a number, not a name. Sticky:
  // tried at a position of the line, never on a copy of the rest of it.
  const LATIN = /(?:[A-Za-z](?:[A-Za-z0-9'&.\-]*[A-Za-z0-9])?|[Ａ-Ｚａ-ｚ](?:[Ａ-Ｚａ-ｚ０-９’＇＆．－]*[Ａ-Ｚａ-ｚ０-９])?)/uy;
  // Laughter written in Latin letters (www, ｗｗｗ, 草w) is no name.
  const LAUGHTER = /^[wｗ]+$/u;
  // A katakana word a single letter is fused to (T|シャツ, J|リーグ, U|ターン, as ICU cuts them).
  const KATAKANA_WORD = /^[\p{Script=Katakana}ーｰ]+$/u;
  // A katakana word, for the viewer who counts katakana as known: two characters or more, ー and ・
  // inside it (ジョン・スミス, コーヒー), never a single character.
  const KATAKANA_RUN = /\p{Script=Katakana}(?:[\p{Script=Katakana}ーｰ]|・(?=\p{Script=Katakana}))+/uy;
  // Place names a YouTuber says, coloured as names: the 47 prefectures with and without their
  // suffix (大分 bare is left out: it is だいぶ far more often), their capitals but Mie's 津, the
  // cities over half a million people, Tokyo's 23 wards with 区 and the ones that are unambiguous
  // bare (not 中央, 港, 北 or 大田), the districts and sights a travel video names (日光, 両国,
  // 新世界 and 高山 are left out as common nouns), the regions, Japan and the countries and cities
  // abroad that come up (チリ is left out: the chili of エビ|チリ and チリ|ソース far more often
  // than Chile).
  // Sorted by their first character; matched longest first.
  const PLACES = new Set([
    "お台場", "さいたま", "アイルランド", "アジア", "アフリカ", "アムステルダム", "アメリカ", "アルゼンチン", "イギリス", "イスタンブール", "イスラエル",
    "イタリア", "イラク", "イラン", "インド", "インドネシア", "ウィーン", "ウクライナ", "エジプト", "オランダ", "オーストラリア", "オーストリア", "カナダ",
    "カンボジア", "ギリシャ", "グアム", "ケニア", "サウジアラビア", "サンフランシスコ", "シアトル", "シカゴ", "シドニー", "シンガポール", "ジャカルタ",
    "スイス", "スウェーデン", "スカイツリー", "スペイン", "ソウル", "タイ", "デンマーク", "トルコ", "トロント", "ドイツ", "ドバイ", "ニセコ",
    "ニュージーランド", "ニューヨーク", "ネパール", "ノルウェー", "ハウステンボス", "ハノイ", "ハワイ", "バルセロナ", "バンクーバー", "バンコク", "パキスタン",
    "パリ", "フィリピン", "フィンランド", "フランス", "ブラジル", "ベトナム", "ベルギー", "ベルリン", "ペルー", "ボストン", "ポルトガル", "ポーランド",
    "マカオ", "マドリード", "マニラ", "マレーシア", "ミャンマー", "ミュンヘン", "ミラノ", "メキシコ", "メルボルン", "モスクワ", "モンゴル", "ヨーロッパ",
    "ラスベガス", "ロサンゼルス", "ロシア", "ロンドン", "ローマ", "ワシントン", "三宮", "三重", "三重県", "三鷹", "上海", "上野", "上高地",
    "下北沢", "世田谷", "世田谷区", "中国", "中央区", "中目黒", "中野", "中野区", "丸の内", "九州", "五反田", "京都", "京都府", "仙台",
    "代官山", "伊勢", "伊豆", "伏見", "伏見稲荷", "会津", "佐世保", "佐渡", "佐賀", "佐賀県", "信州", "倉敷", "八王子", "六本木", "兵庫",
    "兵庫県", "兼六園", "出雲", "函館", "別府", "前橋", "北九州", "北京", "北区", "北朝鮮", "北海道", "北陸", "千代田", "千代田区", "千葉",
    "千葉県", "南アフリカ", "博多", "原宿", "台北", "台東", "台東区", "台湾", "吉祥寺", "名古屋", "和歌山", "和歌山県", "品川", "品川区",
    "四国", "城崎", "埼玉", "埼玉県", "堺", "墨田", "墨田区", "大井町", "大分県", "大手町", "大津", "大田区", "大阪", "大阪城", "大阪府",
    "天王寺", "天神", "天草", "太宰府", "太平洋", "奈良", "奈良公園", "奈良県", "姫路", "姫路城", "宇都宮", "宮古島", "宮城", "宮城県", "宮島",
    "宮崎", "宮崎県", "富士", "富士山", "富山", "富山県", "富良野", "対馬", "小樽", "小豆島", "尾道", "屋久島", "山中湖", "山口", "山口県",
    "山形", "山形県", "山梨", "山梨県", "山陰", "山陽", "岐阜", "岐阜県", "岡山", "岡山県", "岩手", "岩手県", "島根", "島根県", "嵐山",
    "川口", "川崎", "川越", "平泉", "広尾", "広島", "広島県", "御茶ノ水", "徳島", "徳島県", "心斎橋", "志摩", "恵比寿", "愛媛", "愛媛県",
    "愛知", "愛知県", "成田", "押上", "指宿", "文京", "文京区", "新宿", "新宿区", "新橋", "新潟", "新潟県", "日本", "日本橋", "日本海",
    "旭川", "明治神宮", "有楽町", "有馬", "朝鮮", "札幌", "杉並", "杉並区", "東京", "東京タワー", "東京都", "東北", "東大寺", "東海", "松山",
    "松島", "松本", "松江", "板橋", "板橋区", "栃木", "栃木県", "桜島", "梅田", "横浜", "横須賀", "欧米", "歌舞伎町", "水戸", "永田町",
    "汐留", "江ノ島", "江戸川", "江戸川区", "江東", "江東区", "池袋", "沖縄", "沖縄県", "河原町", "河口湖", "洞爺湖", "浅草", "浅草寺", "浜松",
    "淡路島", "清水寺", "渋谷", "渋谷区", "港区", "湘南", "湯布院", "滋賀", "滋賀県", "瀬戸内海", "熊本", "熊本県", "熱海", "琵琶湖", "由布院",
    "甲府", "町田", "登別", "白川郷", "白浜", "皇居", "盛岡", "目黒", "目黒区", "相模原", "知床", "石垣島", "石川", "石川県", "祇園",
    "神奈川", "神奈川県", "神戸", "神田", "福井", "福井県", "福岡", "福岡県", "福島", "福島県", "秋田", "秋田県", "秋葉原", "秩父", "種子島",
    "立川", "竹富島", "箱根", "築地", "米国", "練馬", "練馬区", "美瑛", "群馬", "群馬県", "羽田", "能登", "自由が丘", "舞浜", "船橋",
    "英国", "茨城", "茨城県", "草津", "荒川", "荒川区", "葛飾", "葛飾区", "蒲田", "表参道", "西表島", "豊島", "豊島区", "赤坂", "足立",
    "足立区", "軽井沢", "近畿", "通天閣", "道頓堀", "那覇", "那須", "金沢", "金閣寺", "釜山", "銀座", "錦糸町", "鎌倉", "長崎", "長崎県",
    "長野", "長野県", "関東", "関西", "阿蘇", "難波", "霞が関", "霧島", "青山", "青森", "青森県", "静岡", "静岡県", "韓国", "首里城",
    "香川", "香川県", "香港", "高尾山", "高松", "高知", "高知県", "高野山", "鬼怒川", "鳥取", "鳥取県", "鹿児島", "鹿児島県", "麻布",
  ]);
  const PLACE_MAX_LEN = Math.max(...[...PLACES].map((place) => place.length));
  const PLACE_HEADS = new Set([...PLACES].map((place) => place[0]));
  // What a place name or a kanji or katakana segment of two characters or more takes after it as
  // one name (東京駅, 渋谷区, 品川駅, 名古屋城): the name and the suffix are one blue run when the
  // suffix is the segment after the name (東京|駅, as ICU cuts it) or ends the segment the name
  // began (東京駅 kept whole), so 東京駅前 stays 東京 and 駅前 (ICU cuts 東京|駅前), and 駅舎 after
  // 丸の内 is a word of its own.
  const PLACE_SUFFIXES = new Set([
    "駅", "県", "市", "区", "町", "村", "郡", "都", "府", "島", "山", "川", "湖", "海", "港", "寺", "城", "橋", "線", "湾", "峠", "岬", "滝", "岳",
    "空港", "公園", "神社", "通り", "温泉", "半島", "高原", "海岸", "商店街", "タワー", "ドーム",
  ]);
  // The regexes below scan a line from every character when no bracket follows, so a passage in
  // the word field would cost its length squared. A line this long holds no word (the index drops
  // any over MAX_WORD_LEN, furigana brackets included), and a reading field this long is no
  // reading: a 20-kanji word with <ruby><rp> markup on every kanji fits.
  const MAX_WORD_FIELD_LEN = 8 * MAX_WORD_LEN;
  const MAX_READING_FIELD_LEN = 1000;
  // What stripMarkup() and parsePitch() read of a raw field at most. A field is third-party
  // content (a shared deck), so the tag patterns below stop at the next "<" rather than scanning
  // to the end of the field from every "<" that is never closed, and since an unclosed <rt> still
  // restarts the scan for its end tag, the length itself is bounded too: a word with ruby markup on
  // every kanji, or a {pitch-accents} field drawing four patterns of a ten-mora word (under 8,000
  // characters), is well within it.
  const MAX_FIELD_HTML_LEN = 16000;

  // A tag runs to the next ">" but never across a "<" (a literal "<" inside an attribute value
  // would end it early; Anki's editor writes &lt; for one).
  const TAGS = /<[^<>]*>/g;
  const ENTITIES = /&(nbsp|amp|lt|gt|quot|#39);/gi;
  const ENTITY_TEXT = { nbsp: " ", amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" };
  // Ruby readings, and the parentheses shown around them where ruby is not supported. HTML lets
  // the end tag of <rt> and <rp> be left out before the next <rt>, <rp> or </ruby>
  // (<ruby>食<rt>た</ruby>べる), so the reading runs to the next end tag of any of the three, the
  // rule match.js's RUBY reads by too, so a card's word and its sentence read alike. The end tag
  // goes with the match rather than being left to TAGS (a lookahead): that keeps a field of
  // unclosed <rt> at milliseconds, and TAGS would drop it anyway.
  const RT_RP = /<(?:rt|rp)\b[^<>]*>[\s\S]*?<\/(?:rt|rp|ruby)\b[^<>]*>/gi;
  const HAS_RT = /<rt\b/i;
  const RUBY = /<ruby\b[^<>]*>([\s\S]*?)<\/ruby\s*>/gi;
  // Run on the content of one <ruby>: a reading whose </rt> is left out ends at the next rt or
  // rp tag or at the end of that content.
  const RT = /<rt\b[^<>]*>([\s\S]*?)(?=<\/?(?:rt|rp|ruby)\b|$)/gi;
  const LINE_BREAKS = /<br\s*\/?>|<\/(?:p|div|li|tr)\s*>/gi;
  // Yomitan's furigana format: a space marks where a kanji run starts and its reading follows in
  // brackets (" 食[た]べる", "お 茶[ちゃ]"). $1 is the kanji, $2 the reading.
  const FURIGANA = /\s?(\S+?)\[([^\]]*)\]/g;
  const BRACKETS = /\[[^\]]*\]/g;
  const SPACE = /\s+/g;

  const HIRAGANA = /^\p{Script=Hiragana}$/u;
  const KATAKANA = /^\p{Script=Katakana}$/u;
  const KANJI_OR_KATAKANA = /[\p{Script=Han}\p{Script=Katakana}]/u;
  const HAS_KANJI = /\p{Script=Han}/u;
  const KANJI = /^[\p{Script=Han}々]$/u;
  // A segment the suffix rule may join to a place suffix: kanji or katakana throughout (佐々木,
  // スカイツリー), with ー and 々.
  const KANJI_OR_KATAKANA_RUN = /^[\p{Script=Han}\p{Script=Katakana}ーｰ々]+$/u;
  // What a word never ends before, short of a word boundary: a kanji or katakana continues it
  // (関係, 日本語), and so does ー. Tested on a two-character slice, so a kanji beyond the BMP counts.
  const KANJI_OR_KATAKANA_NEXT = /^[\p{Script=Han}\p{Script=Katakana}ーｰ]/u;
  // ー and ・ belong to neither script (Unicode files them under Common); a reading may hold them.
  const KANA_ONLY = /^[\p{Script=Hiragana}\p{Script=Katakana}ー・ｰ]+$/u;
  // The small kana that glide onto the mora before them; っ and ん are moras of their own.
  const SMALL_KANA = new Set([..."ゃゅょャュョぁぃぅぇぉァィゥェォヮゎ"]);

  function str(value) {
    return value === undefined || value === null ? "" : String(value);
  }

  function stripMarkup(html) {
    return str(html)
      .slice(0, MAX_FIELD_HTML_LEN)
      .replace(RT_RP, "")
      .replace(LINE_BREAKS, "\n")
      .replace(TAGS, "")
      .replace(ENTITIES, (_m, name) => ENTITY_TEXT[String(name).toLowerCase()] || "");
  }

  function plainText(html) {
    return stripMarkup(html).replace(SPACE, " ").trim();
  }

  // The word a card is about: its first line, without furigana. "" when the field holds nothing.
  function plainWord(html) {
    for (const raw of stripMarkup(html).split("\n")) {
      const line = raw.trimStart().slice(0, MAX_WORD_FIELD_LEN);
      const word = line.replace(FURIGANA, "$1").replace(BRACKETS, "").replace(SPACE, " ").trim();
      if (word) return word;
    }
    return "";
  }

  function isHiragana(ch) {
    return HIRAGANA.test(str(ch));
  }

  function isKatakana(ch) {
    return KATAKANA.test(str(ch));
  }

  function isKana(ch) {
    return isHiragana(ch) || isKatakana(ch);
  }

  function isKanji(ch) {
    return KANJI.test(str(ch));
  }

  function hasKanjiOrKatakana(text) {
    return KANJI_OR_KATAKANA.test(str(text));
  }

  function kanaOnly(text) {
    return KANA_ONLY.test(text);
  }

  // The length of what a sticky regex matches at `pos` of `text`, or 0.
  function matchLen(re, text, pos) {
    re.lastIndex = pos;
    const found = re.exec(text);
    return found ? found[0].length : 0;
  }

  function moraCount(kana) {
    let count = 0;
    for (const ch of str(kana)) {
      if (SMALL_KANA.has(ch)) continue;
      if (ch === "ー" || ch === "ｰ" || isKana(ch)) count++;
    }
    return count;
  }

  // The reading a field carries: the <rt> texts of its ruby, the brackets of its furigana, else
  // the field itself. "" unless what is left is kana, so a kanji field never counts as a reading.
  function readingOf(html) {
    let text = str(html);
    if (text.length > MAX_READING_FIELD_LEN) return "";
    if (HAS_RT.test(text)) {
      text = text.replace(RUBY, (_m, inner) => {
        const parts = [];
        for (const found of inner.matchAll(RT)) parts.push(found[1]);
        return parts.length ? parts.join("") : inner;
      });
    }
    const reading = plainText(text).replace(FURIGANA, "$2").replace(SPACE, "");
    return kanaOnly(reading) ? reading : "";
  }

  // Yomitan writes the pattern in one of four ways: {pitch-accent-categories} names it,
  // {pitch-accent-positions} gives the mora the pitch drops after ("[2]", or a list of them),
  // {pitch-accents} draws it in text: one <span> per mora, styled inline since Anki keeps no
  // stylesheet ("display:inline-block;position:relative;"), holding the mora and an empty line
  // <span> ("border-color:currentColor;…") whose style is a top border on a high mora and a right
  // border on the mora the pitch drops after; no right border anywhere is heiban. A nasal mora
  // wraps its kana in a second inline-block <span>, so the moras are counted by their line spans.
  // And {pitch-accent-graphs} draws it as an SVG: one <circle> of radius 15 per mora in order, high
  // at cy 25 and low at 75, the mora the pitch drops after hollow ("fill:none", with a radius-5 dot
  // inside it that is no mora) and a triangle <path> for the particle after the word, which says
  // nothing the circles do not. Several patterns come as <ol><li>…</li></ol>, and the first counts,
  // as with the positions.
  const LIST_ITEM = /<li\b/i;
  const MORA_SPAN = /<span style="[^"<>]*display:\s*inline-block[^"<>]*"[^<>]*>/i;
  const MORA_LINE = /<span style="[^"<>]*border-color:[^"<>]*"[^<>]*>/i;
  const DROP_LINE = /border-right-width/i;
  const SVG_OPEN = /<svg\b/i;
  const SVG_CLOSE = /<\/svg\b/i;
  const CIRCLE = /<circle\b([^<>]*)>/gi;
  const RADIUS = /\br\s*=\s*"?\s*(\d+(?:\.\d+)?)/i;
  const HOLLOW = /fill\s*:\s*none|\bfill\s*=\s*"?\s*none/i;
  // A dot drawn this large is a mora; the one inside a hollow mora is 5.
  const MORA_RADIUS = 10;

  // The drop position and the mora count the first SVG graph in `piece` draws, or null.
  function graphPitch(piece) {
    const open = piece.search(SVG_OPEN);
    if (open < 0) return null;
    const close = piece.slice(open).search(SVG_CLOSE);
    const svg = close < 0 ? piece.slice(open) : piece.slice(open, open + close);
    let moras = 0;
    let n = 0;
    for (const found of svg.matchAll(CIRCLE)) {
      const radius = RADIUS.exec(found[1]);
      if (!radius || Number(radius[1]) < MORA_RADIUS) continue;
      moras++;
      if (n === 0 && HOLLOW.test(found[1])) n = moras;
    }
    return moras ? { n, moras } : null;
  }

  // The drop position and the mora count that markup draws, or null when the value holds none.
  function drawnPitch(raw) {
    for (const piece of raw.split(LIST_ITEM)) {
      const lines = [];
      for (const chunk of piece.split(MORA_SPAN).slice(1)) {
        const line = MORA_LINE.exec(chunk);
        if (line) lines.push(line[0]);
      }
      if (lines.length) return { n: lines.findIndex((line) => DROP_LINE.test(line)) + 1, moras: lines.length };
      const graph = graphPitch(piece);
      if (graph) return graph;
    }
    return null;
  }

  // The pattern of a drop after the n-th mora of `moras`: the count decides between odaka and
  // nakadaka, and an unknown count (-1) reads as nakadaka, the more common of the two.
  function categoryOf(n, moras) {
    if (n === 0) return "heiban";
    if (n === 1) return "atamadaka";
    return n === moras ? "odaka" : "nakadaka";
  }

  const CATEGORY = /heiban|平板|atamadaka|頭高|nakadaka|中高|odaka|尾高/i;
  const CATEGORY_OF = {
    heiban: "heiban",
    平板: "heiban",
    atamadaka: "atamadaka",
    頭高: "atamadaka",
    nakadaka: "nakadaka",
    中高: "nakadaka",
    odaka: "odaka",
    尾高: "odaka",
  };
  const POSITION = /\[(\d+)\]/;
  const DIGITS_ONLY = /^[\d\s]+$/;
  const DOWNSTEP = "ꜜ";

  // A position alone does not name the pattern: the drop after the last mora is odaka, earlier
  // nakadaka, so the mora count comes from the drawn moras, the pitch text, the reading or the
  // word, whichever is kana. Plain text may also mark the drop with ꜜ ("はしꜜ"); kana alone with no
  // mark says nothing (a card whose pitch field holds the bare reading, as old Yomitan templates
  // wrote it, is not heiban for that: 302 of the viewer's cards were, whatever their pattern), and
  // markup that draws nothing this reads is not a pattern.
  function parsePitch(text, reading, word) {
    const raw = str(text).slice(0, MAX_FIELD_HTML_LEN);
    const plain = plainText(raw);
    const named = plain ? CATEGORY.exec(plain) : null;
    if (named) return CATEGORY_OF[named[0].toLowerCase()];
    const drawn = drawnPitch(raw);
    if (drawn) return categoryOf(drawn.n, drawn.moras);
    if (!plain) return null;

    let n = null;
    const position = POSITION.exec(plain);
    const drop = plain.indexOf(DOWNSTEP);
    if (position) n = Number(position[1]);
    else if (DIGITS_ONLY.test(plain)) n = Number(/\d+/.exec(plain)[0]);
    else if (drop >= 0) n = moraCount(plain.slice(0, drop));
    if (n === null) return null;

    let moras = -1;
    const kana = plain.split(DOWNSTEP).join("").replace(SPACE, "");
    if (kanaOnly(kana)) moras = moraCount(kana);
    else if (kanaOnly(str(reading))) moras = moraCount(reading);
    else if (kanaOnly(str(word))) moras = moraCount(word);
    return categoryOf(n, moras);
  }

  const PITCH_FIELD = /pitch|accent|アクセント/i;
  const READING_FIELD = /reading|furigana|読み|よみ/i;
  // A sentence's reading (SentenceFurigana, 例文読み) is not the word's: its mora count would
  // make every odaka word nakadaka on a note type without a reading field for the word.
  const SENTENCE_FIELD = /sentence|文/i;

  // Which of a note's field names a name from the settings means: the name itself, else the one
  // field whose name differs from it in case alone. Note types spell the same field Picture or
  // picture, SentenceAudio or sentenceAudio (Lapis and JPMN capitalise, Eminent does not), and
  // Anki answers a name exactly, so a setting that differed from the note type in case alone found
  // nothing and every mine failed. Two fields differing in case alone leave an inexact name
  // unmatched rather than guessed. The name is trimmed; a blank one names nothing.
  function sameField(names, name) {
    const wanted = str(name).trim();
    if (!wanted) return null;
    if (names.includes(wanted)) return wanted;
    const lower = wanted.toLowerCase();
    const same = names.filter((each) => each.toLowerCase() === lower);
    return same.length === 1 ? same[0] : null;
  }

  // The key of that field in notesInfo's `fields` object, or null.
  function fieldKey(fields, name) {
    if (!fields || typeof fields !== "object") return null;
    return sameField(Object.keys(fields), name);
  }

  // A note's fields as AnkiConnect's notesInfo lists them, lowest `order` first.
  function fieldsInOrder(fields) {
    const list = [];
    for (const [name, field] of Object.entries(fields || {})) {
      if (!field || typeof field !== "object") continue;
      const order = Number(field.order);
      list.push({ name, value: str(field.value), order: Number.isFinite(order) ? order : Infinity });
    }
    return list.sort((a, b) => a.order - b.order);
  }

  // The field the word is read from: the one the settings name, else the first, the same rule
  // as the background's noteSummary().
  function wordFieldOf(list, settings) {
    const wordName = str((settings || {}).ankiWordField).trim();
    if (!wordName) return list.find((field) => field.order === 0);
    const name = sameField(list.map((field) => field.name), wordName);
    return list.find((field) => field.name === name);
  }

  // The pitch fields: the one the settings name, else every field whose name says pitch, in order.
  function pitchFieldsOf(list, settings) {
    const wanted = sameField(list.map((field) => field.name), (settings || {}).ankiPitchField);
    const named = wanted ? list.find((field) => field.name === wanted) : undefined;
    return named ? [named] : list.filter((field) => PITCH_FIELD.test(field.name));
  }

  // Whether a field holds the word's reading rather than a sentence's.
  function isReadingField(field) {
    return READING_FIELD.test(field.name) && !SENTENCE_FIELD.test(field.name);
  }

  // The word's reading, from the lowest-order field whose name says reading and not sentence,
  // `skip` aside.
  function readingFrom(list, skip) {
    const field = list.find((f) => !skip(f) && isReadingField(f));
    return field ? readingOf(field.value) : "";
  }

  // The first pitch field with a readable value decides: a field that draws nothing this reads
  // before a position field must not hide the position. When none of them reads, the reading fields
  // are the last resort, and only for a drawn pattern: Yomitan's {pitch-accents} is often the
  // reading field itself, marked up with the pitch, while a plain reading says nothing.
  function pitchOf(fields, settings) {
    const list = fieldsInOrder(fields);
    const candidates = pitchFieldsOf(list, settings);
    const wordField = wordFieldOf(list, settings);
    const word = wordField ? plainWord(wordField.value) : "";
    for (const pitch of candidates) {
      const found = parsePitch(pitch.value, readingFrom(list, (field) => field === pitch), word);
      if (found) return found;
    }
    for (const field of list) {
      if (candidates.includes(field) || !isReadingField(field)) continue;
      const drawn = drawnPitch(field.value.slice(0, MAX_FIELD_HTML_LEN));
      if (drawn) return categoryOf(drawn.n, drawn.moras);
    }
    return null;
  }

  // Jitendex writes the tag as a title (title="word usually written using kana alone"), JMdict's
  // entity says the same, and Yomitan's plain glossary lists it as `uk` in a parenthesised tag
  // list: "(adv, uk, JMdict (English))". A glossary runs past MAX_FIELD_HTML_LEN and the tag may
  // sit anywhere in it, so the whole value is read, by scans that stay linear on a field of
  // nothing but "(" or "<". Only a list item that is exactly `uk` counts: (UK) and (uk-based)
  // are prose.
  const USUALLY_KANA = "usually written using kana alone";
  // A parenthesised run up to the next parenthesis of either kind: the list before a nested
  // "(English)" is still read, and the lookahead leaves that "(" to start a match of its own.
  const TAG_LIST = /\(([^()]{0,200})(?=[()])/g;

  function usuallyKana(fields) {
    for (const field of fieldsInOrder(fields)) {
      if (field.value.toLowerCase().includes(USUALLY_KANA)) return true;
      for (const found of field.value.matchAll(TAG_LIST)) {
        if (found[1].split(",").some((item) => item.replace(TAGS, "").trim() === "uk")) return true;
      }
    }
    return false;
  }

  // The reading a kanji word is heard and subtitled as, when its dictionary says it is usually
  // written in kana (更に, subtitled さらに). Only then: every note's reading, indexed, measured
  // half false on real subtitles (勝手 in 向かって, 内容 in 出さないように). "" otherwise, and for a
  // word without kanji, which the index holds as it is.
  function kanaReadingOf(fields, settings) {
    const list = fieldsInOrder(fields);
    const wordField = wordFieldOf(list, settings);
    const word = wordField ? plainWord(wordField.value) : "";
    if (!HAS_KANJI.test(word) || !usuallyKana(fields)) return "";
    const pitches = pitchFieldsOf(list, settings);
    const reading = readingFrom(list, (field) => field === wordField || pitches.includes(field));
    return reading && reading !== word && reading.length <= MAX_WORD_LEN ? reading : "";
  }

  // `sets` holds the note ids of the five findNotes queries the background runs on a deck.
  function statusOf(sets, noteId) {
    const has = (name) => {
      const set = sets && sets[name];
      return !!set && typeof set.has === "function" && set.has(noteId);
    };
    if (!has("unsuspended")) return has("suspended") ? "suspended" : null;
    if (has("new")) return "new";
    if (has("learning")) return "learning";
    if (has("review")) return "learned";
    // Unsuspended, yet in none of the three sets. On Anki 2.1.44 and later every unsuspended
    // card's type is new, learn or review, so nothing reaches this line; an older Anki, or a
    // set a search left out, gets the middle guess rather than no colour.
    return "learning";
  }

  const PROGRESS = { new: 0, learning: 1, learned: 2 };

  // Two notes for one word: the one with the least progress decides, and a suspended note only
  // counts when there is no other. "proper" is no card's status: any card beats it.
  function mergeStatus(a, b) {
    const x = STATUSES.includes(a) ? a : null;
    const y = STATUSES.includes(b) ? b : null;
    if (x === null) return y;
    if (y === null) return x;
    if (x === PROPER) return y;
    if (y === PROPER) return x;
    if (x === "suspended") return y;
    if (y === "suspended") return x;
    return PROGRESS[x] <= PROGRESS[y] ? x : y;
  }

  // ------------------------------------------------------------ the matcher

  // Conjugation tables. A dictionary form is cut before its ending and remembered by that stem;
  // in the text, the stem must be followed by a first piece of its kind (the inflected ending)
  // and then by any run of tail pieces (auxiliaries), so 食べる finds 食べました and 書く finds
  // 書かない. A kana-only word needs a stem of two kana at least (かける, しまう, おいしい; いう,
  // する, くる, みる and いい match exactly only: a stem of one kana would be found in every line)
  // and, like its exact form, must end at a word boundary.
  const FIRST_PIECES = {
    suru: ["する", "し", "さ", "せ", "す", "すれ"],
    "i-adj": ["い", "く", "かっ", "けれ", "さ", "そう", "くて", "くない", "ければ"],
    ru: [
      "る", "た", "て", "ない", "なく", "なきゃ", "ます", "まし", "ませ", "ましょ", "たい", "たく", "たかっ", "たら", "たり", "ても", "ちゃ", "とく",
      "そう", "ながら", "なさい", "まい", "られ", "させ", "よう", "れば", "ろ", "よ", "ず", "ん", "ら", "り", "れ", "っ", "なかっ", "なけれ",
    ],
    う: [..."わいうえおっ"],
    く: [..."かきくけこいっ"],
    ぐ: [..."がぎぐげごい"],
    す: [..."さしすせそ"],
    つ: [..."たちつてとっ"],
    ぬ: [..."なにぬねのん"],
    ぶ: [..."ばびぶべぼん"],
    む: [..."まみむめもん"],
  };
  const TAIL_PIECES = [
    "た", "て", "で", "だ", "ない", "なく", "なかっ", "なけれ", "なきゃ", "ます", "まし", "ませ", "ん", "たい", "たく", "たかっ",
    "れ", "れる", "られ", "られる", "せ", "せる", "させ", "させる", "ば", "う", "よ", "よう", "ろ", "る", "い", "けれ", "ず",
    "ちゃ", "じゃ", "てる", "でる", "てい", "でい", "いる", "いた", "いて", "います", "いない", "ましょ", "でし", "でしょ", "です",
    "たら", "だら", "たり", "だり", "ても", "でも", "ながら", "なさい", "まい", "とく", "どく", "いか", "いき", "いく", "いけ", "いこ", "いっ",
    "いただく", "いただき", "いただけ", "いただい", "いただこ", "いただか", "っ", "ー", "だっ",
  ];
  // 使わせていただきました is わ + せ + て + いただき + まし + た: five tails after the first piece.
  const MAX_TAILS = 5;
  const GODAN = new Set([..."うくぐすつぬぶむ"]);
  // The kana an ichidan verb's stem ends in (食べ, 起き, 見せ, 感じ); a godan る verb's stem ends in
  // the a-row (当た, 変わ, 終わ) or a kanji (帰, 走).
  const IE_ROW = /^[いきぎしじちぢにひびぴみりえけげせぜてでねへべぺめれ]$/u;

  // A tail of one kana is also the first kana of many words (だけ, ため, うち, ばかり, ずっと,
  // せい, いい), so every such tail, and the auxiliaries いる, いく and いただく, names what it may
  // follow: a tail or a first piece by its text, a godan first piece by its row (a, e, o) or
  // "onbin", the 音便 kana before た and て, a する verb's さ, せ and す as "suru:さ" and so on.
  // So だ follows ん (食べるんだ) and not る (食べるだけ), た follows 音便, まし and て (書いた,
  // 食べました, 食べてた) and not る or た (食べるため, 食べたため), う follows the o-row (行こう) and
  // not い (高いうち), ん follows the forms it shortens (食べるん, 食べません, 行かん, 食べてん) and
  // not ちゃ (食べてちゃんと). Longer tails follow anything, as before, but for よう: it is the
  // volitional only after the ichidan-like pieces and a する verb's し (食べられよう, 食べていよう,
  // 勉強しよう), and after る, た, ない, the u-row or an adjective it is 様 (食べるように, 行くようだ).
  // A stem that conjugates like an ichidan verb (られ, させ, the passive せ and れ, the potential
  // e-row, いる's い) takes what 食べ takes, except that た after いる's stem is the tail いた (食べて
  // + いた, never て + い + た), so that one rule below keeps 食べていただく from ending inside いただく.
  const GODAN_ROWS = ["a", "i", "u", "e", "o"];
  const ICHIDAN_LIKE = ["e", "られ", "させ", "せ", "れ", "い", "てい", "でい"];
  const AFTER = {};
  const follows = (pieces, prevs) => {
    for (const piece of pieces) AFTER[piece] = new Set(prevs);
  };
  follows(["た", "だ"], ["onbin", "shi", "っ", "し", "かっ", "なかっ", "たかっ", "まし", "でし", "だっ", "そう", "て", "で", "いっ", "いただい", "ん", "e", "られ", "させ", "せ", "れ"]);
  follows(["て", "で", "てい", "でい", "てる", "でる"], ["onbin", "shi", "っ", "し", "まし", "でし", "なく", "たく", "ない", "くない", "て", "で", "いっ", "いただい", "ん", ...ICHIDAN_LIKE]);
  follows(["う"], ["o", "よ", "ろ", "いこ", "いただこ", "ましょ", "でしょ", "ちゃ", "じゃ"]);
  follows(["ず"], ["a", "ら", "いただか", "suru:せ", ...ICHIDAN_LIKE]);
  follows(["ば"], ["e", "れ", "すれ", "いけ", "いただけ", "けれ", "なけれ"]);
  follows(["せ"], ["a", "ら", "suru:さ"]);
  follows(["れ"], ["a", "ら", "suru:さ", "いただけ", ...ICHIDAN_LIKE]);
  follows(["ろ"], ["し", "だ", ...ICHIDAN_LIKE]);
  follows(["る"], ["て", "で", "いけ", "いただけ", ...ICHIDAN_LIKE]);
  follows(["よ"], ["suru:せ"]);
  // The volitional of the ichidan-like pieces, not the potential's (書けよう is no form).
  follows(["よう"], ["し", ...ICHIDAN_LIKE.filter((piece) => piece !== "e")]);
  // The volitional without its う (行こっか, 行こー).
  follows(["っ", "ー"], ["o"]);
  follows(["なきゃ"], ["a", "ら", "し", "く", "たく", "て", "で", "じゃ", ...ICHIDAN_LIKE]);
  follows(["い", "いる", "いた", "いて", "います", "いない", "いか", "いき", "いく", "いけ", "いこ", "いっ"], ["て", "で", "ちゃ", "じゃ"]);
  follows(["いただく", "いただき", "いただけ", "いただい", "いただこ", "いただか"], ["て", "で"]);
  // The copula's past after the appearance auxiliary (楽しそうだった, 降りそうだった).
  follows(["だっ"], ["そう"]);
  follows(
    ["ん"],
    [
      "a", "u", "e", "adj", "ら", "る", "た", "だ", "て", "で", "ない", "くない", "ます", "ませ", "です", "たい", "れ", "れる", "られ", "られる", "せ", "せる",
      "させ", "させる", "suru:せ", "suru:す", "てる", "でる", "いる", "いた", "いない", "いく", "いけ", "いただく", "いただけ", "とく", "どく", "する", "じゃ",
    ],
  );
  // What is no form on its own, so that a span never ends right after it: the stem of いる,
  // いく or いただく (食べてい, 見ていか, 見ていただい), a godan verb's a-row and o-row (行か needs
  // ない, 行こ needs う; the ru piece ら is the a-row of 帰る), its 音便 kana (書い, 行っ, 飲ん: 行い
  // is 行う's noun, 引っかかる is a verb of its own), a する verb's さ, せ and す (勉強さ needs
  // せる, 勉強す is 勉強すべき's), かっ, なかっ and たかっ (they only reach かった: 見たかっこいい is
  // 見た and かっこいい), けれ and なけれ (they only reach ければ), まし and でし (they only reach
  // ました, まして and でした), だっ (it only reaches だった).
  const OPEN_TAILS = new Set([
    "い", "てい", "でい", "いか", "いこ", "いっ", "いただい", "いただこ", "いただか", "a", "o", "onbin", "ら", "suru:さ", "suru:せ",
    "suru:す", "かっ", "なかっ", "たかっ", "けれ", "なけれ", "まし", "でし", "だっ",
  ]);
  // What the 音便 kana may be followed by: the た and て pieces only. Every other tail after it
  // begins another word (行います, 行いたい, 彼の行いです are 行う's, 飲んどけ is 飲む and どけ), so
  // the pieces it reaches are listed rather than what each of them may follow.
  const NEXT = {
    onbin: new Set(["た", "だ", "て", "で", "てる", "でる", "てい", "でい", "ちゃ", "じゃ", "とく", "どく", "たら", "だら", "たり", "だり", "ても", "でも"]),
  };
  // What a piece may not end a span before: the kana that makes it the start of another word or
  // auxiliary instead. ても before ら is 〜てもらう (the end after 食べて remains), たら before しい,
  // しく, しか, しけ or しさ is 〜たらしい (食べた stays), たく before せ, さ or ら is くせに, たくさん
  // or くらい (and た before くさん: 食べてたくさん is 食べて and たくさん, 食べてたくせに keeps 食べてた),
  // いき before な is いきなり, いく before ら is いくら, and いた before だ is いただく: whether the
  // text reaches いただく through its own tails or not, the span never ends inside it.
  const NOT_BEFORE = {
    ても: ["ら"],
    でも: ["ら"],
    たら: ["しい", "しく", "しか", "しけ", "しさ"],
    だら: ["しい", "しく", "しか", "しけ", "しさ"],
    たく: ["せ", "さ", "ら"],
    た: ["くさん"],
    いき: ["な"],
    いく: ["ら"],
    いた: ["だ"],
  };

  // Whether the text at `pos` begins the word that `prev` may not end a span before.
  function startsWordAfter(prev, text, pos) {
    const next = NOT_BEFORE[prev];
    return !!next && next.some((kana) => text.startsWith(kana, pos));
  }

  function firstRole(kind, piece) {
    // An adjective's い is its ending, not いる's stem: 高いだけ is 高い and だけ.
    if (kind === "i-adj" && piece === "い") return "adj";
    // A する verb's さ, せ and す are not the adjective's さ (高さ is a form, 勉強さ is not).
    if (kind === "suru" && (piece === "さ" || piece === "せ" || piece === "す")) return "suru:" + piece;
    // A る verb's っ is the 音便 kana of a godan one (取った), no form alone (取っかかり).
    if (kind === "ru" && piece === "っ") return "onbin";
    if (!GODAN.has(kind)) return piece;
    const row = FIRST_PIECES[kind].indexOf(piece);
    // A す verb's し takes た and て like the 音便 kana (話した), but it is a form alone (話し, the
    // noun) and takes ます (話します), so it has a role of its own.
    if (kind === "す" && row === 1) return "shi";
    return row >= GODAN_ROWS.length ? "onbin" : GODAN_ROWS[row];
  }

  function table(pieces) {
    return Object.freeze({ pieces: new Set(pieces), maxLen: Math.max(...pieces.map((piece) => piece.length)) });
  }

  const FIRST = {};
  for (const [kind, pieces] of Object.entries(FIRST_PIECES)) FIRST[kind] = table(pieces);
  const TAIL = table(TAIL_PIECES);

  function stemOf(word) {
    const least = hasKanjiOrKatakana(word) ? 1 : 2;
    const found = cutStem(word, least);
    // A kana stem that is a particle gets no forms: からかった would be からい's かっ + た, and no
    // form of an adjective or verb in speech begins with から, なら, かな or しか (習う, 叶う and
    // 叱る written in kana match exactly only).
    return found && PARTICLES.has(found.stem) ? null : found;
  }

  function cutStem(word, least) {
    if (word.length >= least + 2 && word.endsWith("する")) return { stem: word.slice(0, -2), kind: "suru" };
    if (word.length < least + 1) return null;
    const last = word.slice(-1);
    if (last === "い") return { stem: word.slice(0, -1), kind: "i-adj" };
    if (last === "る") return { stem: word.slice(0, -1), kind: "ru" };
    if (GODAN.has(last)) return { stem: word.slice(0, -1), kind: last };
    return null;
  }

  // entries: [word, status, pitch] per note. One word from two notes is merged. `known`, the words
  // the viewer marked as known, are learned whatever their cards say (the card's pitch stays), and
  // a known word without a card is found like any deck word, in its forms too. The index holds Maps
  // keyed by the exact word and by the stem, so a position in the text costs a few lookups whatever
  // the size of the deck.
  function buildIndex(entries, known) {
    const exact = new Map();
    const admit = (word) => word && word.length <= MAX_WORD_LEN && !PARTICLES.has(word);
    for (const entry of Array.isArray(entries) ? entries : []) {
      if (!Array.isArray(entry)) continue;
      const word = str(entry[0]).trim();
      if (!admit(word)) continue;
      const status = STATUSES.includes(entry[1]) ? entry[1] : null;
      const pitch = PITCHES.includes(entry[2]) ? entry[2] : null;
      const held = exact.get(word);
      if (held) {
        held.status = mergeStatus(held.status, status);
        if (held.pitch === null) held.pitch = pitch;
      } else {
        // A word without kanji or katakana must end at a word boundary of the text, else ある is
        // found inside あるいは.
        exact.set(word, { word, status, pitch, bounded: !hasKanjiOrKatakana(word) });
      }
    }
    for (const item of Array.isArray(known) ? known : []) {
      const word = str(item).trim();
      if (!admit(word)) continue;
      const held = exact.get(word);
      if (held) held.status = LEARNED;
      else exact.set(word, { word, status: LEARNED, pitch: null, bounded: !hasKanjiOrKatakana(word) });
    }
    const stems = new Map();
    const heads = new Set();
    let maxLen = 0;
    let maxStemLen = 0;
    for (const entry of exact.values()) {
      Object.freeze(entry);
      heads.add(entry.word[0]);
      maxLen = Math.max(maxLen, entry.word.length);
      const found = stemOf(entry.word);
      if (!found) continue;
      const list = stems.get(found.stem);
      if (list) list.push({ entry, kind: found.kind });
      else stems.set(found.stem, [{ entry, kind: found.kind }]);
      maxStemLen = Math.max(maxStemLen, found.stem.length);
    }
    return Object.freeze({ size: exact.size, exact, stems, heads, maxLen, maxStemLen });
  }

  let segmenter = null;

  // Where a word may begin. ICU's dictionary segmentation keeps あるいは in one piece and cuts
  // これは into これ|は; without it, anywhere.
  function wordStarts(text) {
    const s = str(text);
    const starts = new Set([0]);
    if (segmenter === null) {
      try {
        segmenter =
          typeof Intl !== "undefined" && typeof Intl.Segmenter === "function"
            ? new Intl.Segmenter("ja", { granularity: "word" })
            : false;
      } catch (err) {
        segmenter = false;
      }
    }
    if (segmenter) {
      try {
        for (const piece of segmenter.segment(s)) starts.add(piece.index);
        return starts;
      } catch (err) {
        // Fall through: every index.
      }
    }
    for (let i = 1; i < s.length; i++) starts.add(i);
    return starts;
  }

  // Every end reachable from `pos`, after the piece `prev`, with up to `left` tail pieces, added
  // to `ends`. Taking the longest piece each time is not enough (泳いでいる is で+いる, not
  // でい+る), so every split is tried; the table is small and the depth is five.
  function tailEnds(text, pos, prev, left, ends) {
    if (!OPEN_TAILS.has(prev) && !startsWordAfter(prev, text, pos)) ends.add(pos);
    if (left === 0) return;
    const next = NEXT[prev];
    for (let len = Math.min(TAIL.maxLen, text.length - pos); len >= 1; len--) {
      const piece = text.slice(pos, pos + len);
      if (!TAIL.pieces.has(piece)) continue;
      if (next && !next.has(piece)) continue;
      const after = AFTER[piece];
      if (after && !after.has(prev)) continue;
      tailEnds(text, pos + len, piece, left - 1, ends);
    }
  }

  // Whether `stop` is the end of the text or a word boundary.
  function atBoundary(text, stop, starts) {
    return stop === text.length || starts.has(stop);
  }

  // The start of the segment holding the character at `pos`, and the end of the segment beginning
  // at `pos` (the next boundary, or the end of the text).
  function segmentStart(pos, starts) {
    let seg = pos;
    while (seg > 0 && !starts.has(seg)) seg--;
    return seg;
  }

  function segmentEnd(text, pos, starts) {
    let end = pos + 1;
    while (end < text.length && !starts.has(end)) end++;
    return end;
  }

  // Whether `stop`, inside a segment, cuts a particle ICU recognised (電話|しか, 水|しか): the form
  // that would end there is no form (電話し, 話し), the segment is the particle. Not where what
  // follows the segment makes the particle the start of another word (食|べ|たら|しく|ない: たら
  // before しく is らしい's, and 食べた ends inside it as NOT_BEFORE means it to).
  function insideParticle(text, stop, starts) {
    const seg = segmentStart(stop, starts);
    const end = segmentEnd(text, seg, starts);
    const piece = text.slice(seg, end);
    return PARTICLES.has(piece) && !startsWordAfter(piece, text, end);
  }

  // Where a form of a bounded word beginning at `from` may end: at a boundary, or inside the
  // segment ICU made of the form's ending and the particle after it (わか|っ|たよ, かけ|たよ,
  // でき|ますよ, ちがい|ますよ: the ending never ends at a boundary there), that is when the
  // segment holding `stop` begins after `from`, is no particle itself and what remains of it is a
  // particle. A segment beginning at `from` is no such fusion: ことば, あいだ, はなし and
  // こんにちは are one segment with こと, あい, はな and こんにち, and a card for one of those
  // paints none of them. The exact word has no ending to be fused and ends at a boundary only (はし
  // in は|しか, as ICU cuts it).
  function boundedEnd(text, from, stop, starts) {
    if (atBoundary(text, stop, starts)) return true;
    const seg = segmentStart(stop, starts);
    return seg > from && !insideParticle(text, stop, starts) && particleShapes(text, stop, starts).length > 0;
  }

  // Whether a span may end at `stop`: at the end of the text, at a word boundary, or before
  // anything but a kanji, katakana or ー, and never inside a particle. ICU keeps a compound in one
  // segment, so 関 is not coloured in 関係, 飲み not in 飲み物 and 日本 not in 日本語, while 見た
  // may end before 犬 (見|た|犬) and 電話 before 番号 (電話|番号).
  function endsWord(text, stop, starts) {
    if (atBoundary(text, stop, starts)) return true;
    return !KANJI_OR_KATAKANA_NEXT.test(text.slice(stop, stop + 2)) && !insideParticle(text, stop, starts);
  }

  // Where a form of the word whose stem ends at `pos` ends, or -1 when the text there is no form
  // of it: the furthest end the pieces reach that a word may end at. A する verb is also its noun
  // alone (勉強 in 勉強が), and so is an ichidan verb's stem, which ends in an i-row or e-row kana,
  // at a word boundary (食べ in 食べに行く, 助け in 助けを呼ぶ, 考え, 流れ: the 連用形 is the noun).
  // The boundary keeps it out of a compound ICU holds together (見せ in 見せかけ, 生き in 生きがい);
  // a godan る verb's stem ends in the a-row (当た, 変わ) and is no form (its noun is the り piece,
  // 当たり, found through the tables like 話し and 動き); every other kind needs its first piece,
  // since a bare stem ending in a kanji is another word (走 in 走者, 見 in 見物). A `bounded`
  // entry (a kana-only word, beginning at `from`) gets no bare `ru` stem (its `suru` noun stays,
  // at a boundary: びっくり in びっくり|だ), and its forms end where boundedEnd() admits: ICU cuts
  // a kana run it does not know into single kana (い|れ|ば, かけ|ら), where かけ would be かける's
  // noun and いれ いれる's at every such cut.
  function continuationEnd(text, from, pos, kind, starts, bounded) {
    const first = FIRST[kind];
    const ends = new Set();
    if (kind === "suru" || (kind === "ru" && !bounded && IE_ROW.test(text[pos - 1]) && atBoundary(text, pos, starts))) ends.add(pos);
    for (let len = Math.min(first.maxLen, text.length - pos); len >= 1; len--) {
      const piece = text.slice(pos, pos + len);
      if (!first.pieces.has(piece)) continue;
      // 行く is the one く verb whose 音便 is っ alone (行った): 行い is 行う's (行いたい, 行いました),
      // and every other く verb takes い (歩いた, 書いた), so its っ is another word's (あるって is
      // ある and the quotative って, not あるく's; はたらって is no form).
      if (piece === "い" && kind === "く" && text[pos - 1] === "行") continue;
      if (piece === "っ" && kind === "く" && text[pos - 1] !== "行") continue;
      tailEnds(text, pos + len, firstRole(kind, piece), MAX_TAILS, ends);
    }
    let best = -1;
    for (const end of ends) if (end > best && (bounded ? boundedEnd(text, from, end, starts) : endsWord(text, end, starts))) best = end;
    return best;
  }

  // The words holding a kanji or katakana that are no する noun, although する's forms follow them
  // as often as any noun's: time words, adverbs and pronouns (何してるの, 全然しない, 今日します,
  // 一番したい), a counter (4台しちゃった, 一回した), a single kanji (顔する, 数する: 損する and
  // 得する, the few single-kanji する nouns, stay two runs), and an adverb or adjective ending in a
  // particle's kana (何かしたい, 何もしない, 後でします, 静かにして). The する nouns of the viewer's
  // lines are two kanji or more but for お願い (and 見た目 of 見た目してる, which is none); a noun
  // ending in し is one (引っ越しします, 夜更かしした), so 少し is listed by name.
  const NOT_SURU = new Set([
    "明日", "昨日", "来週", "来月", "来年", "先週", "先月", "去年", "昨年", "次回", "前回", "最近", "最初", "最後", "一番", "一回", "一度",
    "全然", "絶対", "結構", "多分", "本当", "全部", "大分", "随分", "一応", "結局", "普通", "普段", "実際", "当然", "大体", "意外", "案外",
    "早速", "少し", "自分", "皆", "皆さん", "皆様",
  ]);
  const NOT_SURU_HEAD = /^[何毎今]/u;
  const NOT_SURU_END = /[かもでにとは]$/u;

  // Whether the exact word `entry` may run on over する's forms: it holds a kanji or katakana, is
  // no single character, and is none of the words above. A word that ends in する has its own forms
  // through the stems.
  function suruNoun(entry) {
    const word = entry.word;
    if (entry.bounded || word.endsWith("する") || [...word].length < 2) return false;
    return !NOT_SURU.has(word) && !NOT_SURU_HEAD.test(word) && !NOT_SURU_END.test(word);
  }

  // The longest word found at `i`; on a tie the exact word beats a conjugation. A kana-only word
  // found whole also beats a form of itself that adds particles alone (the form is then the word
  // and its tails: です, でしょう, んだ): a kana noun ending in a verb's kana has a stem to the
  // tables (いくつ, きょう, けっこう, ふつう, ほんとう), and its copula would else join its run and
  // carry its pitch overbar (いくつです), while a kana verb loses nothing, since the copula is no
  // part of it either way (わかる|んだ, おいしい|です). A kanji word keeps the form (食べるでしょう
  // is one run). A する noun (suruNoun()) found whole runs on over する's pieces and their tails
  // (勉強している, お願いします, スタートしました: the noun is the verb's, whether or not a card
  // holds the verb), when they reach past a lone し, which is the conjunctive particle as often as
  // する's 連用形 (高いし, 食べるし), and only when no form of a deck word reaches as far: a card
  // for the verb has its own forms (思い出して is 思い出す's, not 思い出's with する). The run never
  // ends on ちゃ or じゃ before っ (勉強しちゃった, 渋滞しちゃってる): the tables cannot follow
  // ちゃった, and a run ending inside it would keep a card for ちゃう from its word; the noun stays
  // alone.
  function matchAt(text, i, index, starts) {
    const remaining = text.length - i;
    let end = i;
    let found = null;
    for (let len = Math.min(index.maxLen, remaining); len >= 1; len--) {
      const entry = index.exact.get(text.slice(i, i + len));
      if (!entry) continue;
      const stop = i + len;
      if (entry.bounded ? !atBoundary(text, stop, starts) : !endsWord(text, stop, starts)) continue;
      end = stop;
      found = entry;
      break;
    }
    const exact = found;
    const exactEnd = end;
    for (let len = Math.min(index.maxStemLen, remaining); len >= 1; len--) {
      const list = index.stems.get(text.slice(i, i + len));
      if (!list) continue;
      for (const { entry, kind } of list) {
        const stop = continuationEnd(text, i, i + len, kind, starts, entry.bounded);
        if (stop <= end) continue;
        if (entry === exact && entry.bounded && particlesOnly(text, exactEnd, stop, starts)) continue;
        end = stop;
        found = entry;
      }
    }
    if (exact && suruNoun(exact)) {
      const stop = continuationEnd(text, i, exactEnd, "suru", starts, false);
      const cut = text[stop] === "っ" && (text.endsWith("ちゃ", stop) || text.endsWith("じゃ", stop));
      if (stop > end && stop > exactEnd + (text[exactEnd] === "し" ? 1 : 0) && !cut) {
        end = stop;
        found = exact;
      }
    }
    return found ? { end, entry: found } : null;
  }

  // The lengths of the entries of PARTICLES at `pos` that end at a word boundary, longest first
  // (には and に in 本|に|は, で in 中|で; not に in 猫|にんじん, not と in 食べる|という).
  // particlesOnly() and boundedEnd() ask where a word's own form ends, particleAt() where a
  // particle counted as known is.
  function particleShapes(text, pos, starts) {
    const lens = [];
    for (let len = Math.min(PARTICLE_MAX_LEN, text.length - pos); len >= 1; len--) {
      if (!PARTICLES.has(text.slice(pos, pos + len))) continue;
      if (atBoundary(text, pos + len, starts)) lens.push(len);
    }
    return lens;
  }

  // Whether the text from `from` to `to` is such particles in a row and nothing else (です, ですか,
  // でしょう, んだ; `to` is a form's end, a few pieces past `from` at most).
  function particlesOnly(text, from, to, starts) {
    if (from >= to) return from === to;
    return particleShapes(text, from, starts).some((len) => particlesOnly(text, from + len, to, starts));
  }

  // The word boundaries the matcher goes by: `starts`, plus the index after a て or で that ICU
  // fused with the く of the auxiliary behind it (TE_FORM: 食|べ|てく|れ|た), where a word may
  // begin and a bounded one end, and the copula's seams (copulaSeam()). `starts` itself is never
  // written (the content script keeps it per cue): the set is copied when there is something to
  // add.
  function boundsOf(text, starts) {
    let bounds = starts;
    const add = (pos) => {
      if (bounds === starts) bounds = new Set(starts);
      bounds.add(pos);
    };
    for (const pos of starts) {
      if (TE_FORM.has(text[pos]) && text[pos + 1] === "く" && !starts.has(pos + 1)) add(pos + 1);
      for (const seam of copulaSeams(text, pos, starts)) add(seam);
    }
    return bounds;
  }

  // The auxiliaries ICU fuses with the copula after them (そうだ, そうです, そうだね in Firefox:
  // 良さ|そうだ|な, 大丈夫|そうだ), so that the copula never begins a segment there.
  const COPULA_HEADS = ["そう", "よう", "みたい"];
  // The copula ICU cuts in two, a lone だ or で and the rest fused with what follows (Firefox:
  // 大変|だ|ったね, 学生|で|したね): the entries of PARTICLES that begin with that kana.
  const COPULA_CUT = [...PARTICLES].filter((piece) => piece.length > 1 && (piece[0] === "だ" || piece[0] === "で"));

  // The lengths of the entries of PARTICLES that `piece` is made of, in order, longest first where
  // it can be cut more than one way (だね: だ, ね), or null.
  function particleSplit(piece) {
    if (!piece) return [];
    for (let len = Math.min(PARTICLE_MAX_LEN, piece.length); len >= 1; len--) {
      if (!PARTICLES.has(piece.slice(0, len))) continue;
      const rest = particleSplit(piece.slice(len));
      if (rest) return [len, ...rest];
    }
    return null;
  }

  // The indices inside the segment at `pos` where ICU hid the copula's seams, none when it hid
  // none: after an auxiliary of COPULA_HEADS fused with particles only (そう|だ, そう|だ|ね), or
  // after the copula's second half fused with particles only (だ|った|ね, で|した|ね, だ|っ|たん), and between
  // those particles. What follows must be particles alone, so no word ICU knows is ever cut
  // (ようやく, そうじ; みたいな is みたい and な, both kept as they were).
  function copulaSeams(text, pos, starts) {
    const end = segmentEnd(text, pos, starts);
    const cuts = (from, stop) => {
      const lens = particleSplit(text.slice(from, stop));
      if (!lens) return [];
      const seams = [from];
      for (const len of lens.slice(0, -1)) seams.push(seams[seams.length - 1] + len);
      return seams;
    };
    const head = COPULA_HEADS.find((piece) => text.startsWith(piece, pos) && pos + piece.length < end);
    if (head) return cuts(pos + head.length, end);
    if (end !== pos + 1 || end >= text.length) return [];
    for (const piece of COPULA_CUT) {
      const stop = pos + piece.length;
      if (!text.startsWith(piece, pos) || starts.has(stop) || stop >= text.length) continue;
      // The segment the piece ends inside: the next one (だ|ったね) or one after it (だ|っ|たん).
      let next = end;
      while (next < stop) next = segmentEnd(text, next, starts);
      const seams = cuts(stop, next);
      if (seams.length) return seams;
    }
    return [];
  }

  // The end of the suffix that joins a name ending at `end`: what remains of the segment there
  // (東京駅 kept whole by ICU), or the segment beginning there (東京|駅), when it is one of
  // PLACE_SUFFIXES; else -1.
  function suffixEnd(text, end, starts) {
    if (end >= text.length) return -1;
    const stop = segmentEnd(text, end, starts);
    return PLACE_SUFFIXES.has(text.slice(end, stop)) ? stop : -1;
  }

  // The end of the place name at `pos`, the longest entry of PLACES there that ends a word or takes
  // a suffix (東京都, 東京駅, 大阪城), or -1.
  function placeAt(text, pos, starts) {
    if (!PLACE_HEADS.has(text[pos])) return -1;
    for (let len = Math.min(PLACE_MAX_LEN, text.length - pos); len >= 1; len--) {
      const stop = pos + len;
      if (!PLACES.has(text.slice(pos, stop))) continue;
      const suffix = suffixEnd(text, stop, starts);
      if (suffix > stop) return suffix;
      if (endsWord(text, stop, starts)) return stop;
    }
    return -1;
  }

  // The end of the Latin run of `len` characters at `pos` as a name, or -1 for laughter: a single
  // letter takes the katakana word it is fused to (Tシャツ, Jリーグ are one word, not a letter and a
  // word).
  function latinEnd(text, pos, len, starts) {
    if (LAUGHTER.test(text.slice(pos, pos + len))) return -1;
    if (len === 1 && isKatakana(text[pos + 1])) {
      const stop = segmentEnd(text, pos + 1, starts);
      if (KATAKANA_WORD.test(text.slice(pos + 1, stop))) return stop;
    }
    return pos + len;
  }

  // The end of the name at `pos` that needs no card, or -1: Latin text, a place name, or, when
  // `pair` (no deck word begins at `pos`), a kanji or katakana segment of two characters or more
  // with a suffix segment after it (佐々木|駅, スカイツリー|駅; a single kanji takes none). The
  // pair is a guess, and a card for its first segment says otherwise: 昨日|海, 結構|山, 地元|駅
  // and 天然|温泉 are a word and a noun, and each keeps its own colour.
  function nameAt(text, pos, starts, pair) {
    const latin = matchLen(LATIN, text, pos);
    if (latin) return latinEnd(text, pos, latin, starts);
    const place = placeAt(text, pos, starts);
    if (place > pos) return place;
    if (!pair) return -1;
    const seg = segmentEnd(text, pos, starts);
    if (seg - pos < 2 || !KANJI_OR_KATAKANA_RUN.test(text.slice(pos, seg))) return -1;
    return suffixEnd(text, seg, starts);
  }

  // The end of the katakana word at `pos`, two characters or more, cut before a deck word that
  // begins inside it (コーヒー|カップ with カップ in the deck), or -1.
  function katakanaAt(text, pos, starts, tryAt) {
    let end = pos + matchLen(KATAKANA_RUN, text, pos);
    for (let at = pos + 1; at < end; at++) {
      if (starts.has(at) && tryAt(at)) {
        end = at;
        break;
      }
    }
    return end - pos >= 2 ? end : -1;
  }

  // Whether the segment at `pos` is a た or て alone, or with particles fused to it (猫|が|で|た,
  // 猫|が|で|たよ; not たくさん).
  function pastAlone(text, pos, starts) {
    const rest = text.slice(pos + 1, segmentEnd(text, pos, starts));
    return !rest || PARTICLES.has(rest);
  }

  // Whether the particle-shaped `piece` at `pos` is a piece of a verb ICU cut up rather than a
  // particle (see PARTICLE_NOT_BEFORE and INFLECTIONS). `afterPlain`: plain text ends at `pos`.
  function shreddedVerb(text, pos, piece, starts, afterPlain) {
    const before = str(text[pos - 1]);
    if (afterPlain && INFLECTIONS.has(piece) && isHiragana(before)) return true;
    const end = pos + piece.length;
    const next = text.slice(end, end + 2);
    const one = piece.length === 1;
    const quoted = (piece === "ね" || piece === "よ") && text.startsWith("って", end) && atBoundary(text, end + 2, starts);
    if (one && piece !== "だ" && piece !== "の" && next[0] === "っ" && !quoted) return true;
    if (one && (next[0] === "た" || next[0] === "て") && pastAlone(text, pos + 1, starts)) return true;
    const heads = PARTICLE_NOT_BEFORE[piece];
    if (heads && heads.some((kana) => next.startsWith(kana))) return true;
    const segments = PARTICLE_NOT_BEFORE_SEGMENT[piece];
    if (segments && end < text.length && segments.includes(text.slice(end, segmentEnd(text, end, starts)))) return true;
    // The second half of such a pair is no particle either (the に of な|に, the さ of 情け|な|さ).
    const pair = afterPlain && starts.has(pos - 1) ? PARTICLE_NOT_BEFORE_SEGMENT[before] : null;
    if (pair && pair.includes(text.slice(pos, segmentEnd(text, pos, starts)))) return true;
    if ((one || piece === "っけ") && GLIDES.has(next[0])) return true;
    if (one && isKana(next[0]) && !PARTICLES.has(next[0]) && starts.has(end + 1) && SMALL_KANA.has(str(next[1]))) return true;
    if (isKanji(before)) {
      if (piece === "ん") return true;
      if (one && A_ROW_OKURIGANA.has(piece) && NEGATIVE_HEAD.test(next) && before !== "何" && before !== "誰") return true;
    }
    if ((piece[0] === "だ" || piece[0] === "で") && before === "ん" && isKanji(text[pos - 2])) return true;
    const contracted = text.endsWith("ちゃ", pos) || text.endsWith("じゃ", pos);
    return piece === "って" && A_ROW_BEFORE_TE.test(before) && (afterPlain || contracted);
  }

  // The end of the particle at `pos` that counts as known, or -1: the quotative with いう, else the
  // longest entry of PARTICLES there that ends at a word boundary and is no piece of a verb ICU cut
  // up (に, not にも, in 猫|に|も|ら|っ|た), else a particle of one kana ICU fused with the い of
  // いる after it (人|がい|た; never at the end of the text, 猫|がい).
  function particleAt(text, pos, starts, afterPlain) {
    const phrase = QUOTE_PHRASES.find((piece) => text.startsWith(piece, pos) && atBoundary(text, pos + piece.length, starts));
    if (phrase) return pos + phrase.length;
    for (const len of particleShapes(text, pos, starts)) {
      if (!shreddedVerb(text, pos, text.slice(pos, pos + len), starts, afterPlain)) return pos + len;
    }
    const piece = text[pos];
    const iru = pos + 2;
    if (!IRU_PARTICLES.has(piece) || text[pos + 1] !== "い" || starts.has(pos + 1)) return -1;
    if (piece === "と" && ONBIN_KANA.has(text[pos - 1])) return -1;
    if (iru >= text.length || !starts.has(iru) || !IRU_ENDINGS.some((ending) => text.startsWith(ending, iru))) return -1;
    return shreddedVerb(text, pos, piece, starts, afterPlain) ? -1 : pos + 1;
  }

  // The grammar words a learner reads past like the particles, counted as known with them (the
  // `particles` option): the verbs that carry the grammar (ある, いる, おる, みる, する, くる, なる,
  // いく, おく, しまう, くれる, もらう, あげる, やる) in their common forms, since a verb whose stem
  // is one kana matches its exact form only; the auxiliaries of appearance and hearsay; the こそあど
  // words, the formal nouns and the everyday adverbs. A deck word at the same start still wins: a
  // card says more than this list.
  const godanRu = (stem) => ["る", "った", "って", "ってる", "り", "ります", "りました", "りません", "らない", "らなかった", "れば", "ろう"].map((end) => stem + end);
  const ichidan = (stem) => ["る", "た", "て", "ます", "ました", "ません", "ない", "なかった", "れば", "よう", "ろ", "たい", "てる"].map((end) => stem + end);
  const GRAMMAR_WORDS = [
    ...godanRu("あ").filter((form) => !form.startsWith("あら")), "ない", "なかった",
    ...godanRu("お"), ...godanRu("な"), ...godanRu("や"),
    ...ichidan("い"), ...ichidan("み"),
    "する", "した", "して", "します", "しました", "しません", "しない", "しなかった", "すれば", "しよう", "しろ", "したい", "してる",
    "くる", "きた", "きて", "きます", "きました", "きません", "こない", "こなかった", "くれば", "こよう", "こい", "きたい", "きてる",
    "いく", "いった", "いって", "いき", "いきます", "いきました", "いかない", "いけば", "いこう",
    "おく", "おいた", "おいて", "おきます", "おかない",
    "しまう", "くれる", "もらう", "あげる",
    "そう", "よう", "みたい", "らしい",
    "これ", "それ", "あれ", "どれ", "この", "その", "あの", "どの", "ここ", "そこ", "あそこ", "どこ", "こう", "ああ", "どう",
    "どうしよう", "こんな", "そんな", "あんな", "どんな", "こちら", "そちら", "あちら", "どちら", "何", "なに", "なん",
    "こと", "もの", "ため", "わけ", "はず", "ところ", "とき", "ほう",
    "まだ", "まだまだ", "もう", "また", "よく", "もっと", "ずっと", "ちょっと", "とても", "すごく", "やっぱり", "やはり", "きっと", "たぶん", "いい",
  ];
  let grammarIndex = null;

  // The end of the grammar word at `pos` (GRAMMAR_WORDS), or -1. Not after a single kanji or kana
  // of plain text ICU cut off, whose kana are that word's okurigana (書|い|た, 思|い|ます,
  // ご|ざ|い|ます), not いる's or ある's. Nor なっ after い or だ, which is the sentence-final な
  // before a quotative って (すごいなって, 好きだなって), never なる. `afterPlain`: plain text ends
  // at `pos`.
  function grammarAt(text, pos, starts, afterPlain) {
    if (afterPlain && starts.has(pos - 1) && (isKanji(text[pos - 1]) || isKana(text[pos - 1]))) return -1;
    if (text.startsWith("なっ", pos) && (text[pos - 1] === "い" || text[pos - 1] === "だ")) return -1;
    if (!grammarIndex) grammarIndex = buildIndex(GRAMMAR_WORDS.map((word) => [word, LEARNED, null]));
    const hit = matchAt(text, pos, grammarIndex, starts);
    return hit ? hit.end : -1;
  }

  // The text as runs, in order: a matched run carries its entry's status and pitch, the text
  // between matches is one run with neither. `starts` is the set of indices a word may begin at;
  // `opts.particles` and `opts.katakana` count the particles and the katakana words as known,
  // `opts.names` draws the names "proper". At a word boundary, in this order of precedence:
  //  - a deck word (or a known one), with the honorific prefix ICU cut off it (お in お|風呂, its
  //    status and no pitch, before the word's own run) or the quotative that fronts いう inside one
  //    segment (って in っていう: with the particles a particle, learned; without them the status
  //    of the run it follows, plain when none does or that run is a name);
  //  - a name (nameAt(): Latin text, a place, a suffix joined to it), which takes the place of a
  //    deck word only when it is longer: 東京駅 over 東京, 丸の内 over 丸, while 東京 alone stays
  //    the deck's. With the option it is "proper"; without it the name is still taken whole, so
  //    no deck word is found inside it (a card for 駅 says nothing about 東京駅), but it stays
  //    plain text, no card standing behind it, save its katakana head, which the katakana option
  //    still counts as known (アメリカ, スカイツリー of スカイツリー駅); the particle option never
  //    takes a name;
  //  - with the option, a katakana word, learned;
  //  - with the option, a particle, learned, each a run of its own and never the colour of the word
  //    before it (まで after 領域 says nothing about 領域's card). Without the option the word
  //    alone takes the colour and the particles stay plain text.
  function markWords(text, index, starts, opts) {
    const s = str(text);
    const runs = [];
    const plain = (piece) => runs.push({ text: piece, status: null, pitch: null });
    if (!s) return runs;
    const katakana = !!(opts && opts.katakana);
    const particles = !!(opts && opts.particles);
    const names = !!(opts && opts.names);
    const bounds = boundsOf(s, starts instanceof Set ? starts : starts ? new Set(starts) : wordStarts(s));
    const usable = !!index && index.size > 0;
    const heads = usable ? index.heads : null;
    const tryAt = (pos) => (usable && (!heads || heads.has(s[pos])) ? matchAt(s, pos, index, bounds) : null);
    let i = 0;
    // Where the text not yet in a run begins: the end of the last coloured run, and its status.
    let from = 0;
    let last = null;
    // The text before `start` as plain text, then the run from `start` to `end`.
    const colour = (start, end, status, pitch) => {
      if (from < start) plain(s.slice(from, start));
      runs.push({ text: s.slice(start, end), status, pitch });
      from = end;
      last = status;
    };
    // The word at the start `pos`: the deck word there, else the one after an honorific prefix
    // (ICU cut it off: お|風呂, not お前) or, for いう alone, a quotative (the word ends the
    // segment: いう in っていう, not in そういう), with the length of what fronts it and the colour
    // that takes.
    const wordAt = (pos) => {
      let hit = tryAt(pos);
      if (hit) return { lead: 0, status: null, hit };
      if (HONORIFICS.has(s[pos]) && bounds.has(pos + 1)) {
        hit = tryAt(pos + 1);
        if (hit) return { lead: 1, status: hit.entry.status, hit };
      }
      const quote = QUOTATIVES.find((piece) => s.startsWith(piece, pos));
      if (quote) {
        hit = tryAt(pos + quote.length);
        if (hit && hit.entry.word === QUOTED_WORD && atBoundary(s, hit.end, bounds)) {
          const status = particles ? LEARNED : from === pos && last !== PROPER ? last : null;
          return { lead: quote.length, status, hit };
        }
      }
      return null;
    };
    // With the option, where a run ends inside a segment: the particle or grammar word ICU fused
    // to it, counted as known (広さ in Firefox's 広|さも|ある, whose も boundedEnd() let the word
    // end before; が in 人|がい|た, whose いた is いる's). Returns where the loop goes on.
    const followOn = (pos) => {
      if (!particles || bounds.has(pos) || pos >= s.length) return pos;
      const stop = Math.max(particleAt(s, pos, bounds, false), grammarAt(s, pos, bounds, false));
      if (stop <= pos) return pos;
      colour(pos, stop, LEARNED, null);
      return followOn(stop);
    };
    while (i < s.length) {
      if (!bounds.has(i)) {
        i++;
        continue;
      }
      const found = wordAt(i);
      const name = nameAt(s, i, bounds, !found);
      if (found && name <= found.hit.end) {
        const { lead, hit } = found;
        if (lead && found.status !== null) colour(i, i + lead, found.status, null);
        colour(i + lead, hit.end, hit.entry.status, hit.entry.pitch);
        i = followOn(hit.end);
        continue;
      }
      // A name without its colour is still taken whole, so no deck word is found inside it, and
      // joins the plain text around it; only the katakana rule may still colour its katakana head
      // (アメリカ), since hiding the names must not take back what that switch asked for.
      if (name > i && !names) {
        const kata = katakana ? katakanaAt(s, i, bounds, tryAt) : -1;
        if (kata > i) colour(i, Math.min(kata, name), LEARNED, null);
        i = name;
        continue;
      }
      let end = name;
      let status = PROPER;
      if (end <= i && katakana) {
        end = katakanaAt(s, i, bounds, tryAt);
        status = LEARNED;
      }
      if (end <= i && particles) {
        end = Math.max(particleAt(s, i, bounds, from < i), grammarAt(s, i, bounds, from < i));
        status = LEARNED;
      }
      if (end > i) {
        colour(i, end, status, null);
        i = followOn(end);
        continue;
      }
      i++;
    }
    if (from < s.length) plain(s.slice(from));
    return runs;
  }

  return Object.freeze({
    STATUSES,
    PITCHES,
    MAX_WORD_LEN,
    plainText,
    plainWord,
    isKana,
    isHiragana,
    isKatakana,
    hasKanjiOrKatakana,
    moraCount,
    readingOf,
    fieldKey,
    parsePitch,
    pitchOf,
    usuallyKana,
    kanaReadingOf,
    statusOf,
    mergeStatus,
    buildIndex,
    wordStarts,
    markWords,
  });
})();

// The Node tests require this file directly; Firefox has no `module`.
if (typeof module !== "undefined" && module.exports) module.exports = SHISUKO_WORDS;
