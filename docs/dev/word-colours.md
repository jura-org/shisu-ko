# Word colours

The card-status and pitch-accent colouring of subtitle words: `addon/words.js`, the deck index in `addon/background.js`, the drawing in `addon/content.js` and the popup controls.

## How word colours work

Two opt-in colourings of the words of a line, both off by default: `cardStatus` colours a word by
the state of its Anki card (`data-status`: `learned` green, `learning` yellow, `suspended`
orange, `new` red, and `proper` blue for a name or Latin text, which no card has, only with
`properNames` on; colours as
custom properties on `.shisuko-root` in `content.css`), `pitchAccent` draws an overline in the
colour of its pitch accent pattern (`data-pitch`: `heiban` blue, `atamadaka` red, `nakadaka`
orange, `odaka` green). `cardStatusDeck` names the deck (empty is automatic: the deck the last
mined card went to; nothing mined and nothing chosen means no deck, so a collection is never
searched by guesswork), `ankiPitchField` the note field holding the pitch (empty: found by
name). Four settings refine what `cardStatus` shows, and none of them asks Anki: `knownWords`
(the viewer's own list, one word per line, `learned` whatever the card says; Alt+Shift+K, the
`mark-known` command, puts the word under the pointer on it or takes it off), `particlesKnown`
(off by default; on, a particle or a grammar word, `GRAMMAR_WORDS`, is `learned`), `katakanaKnown` (a katakana word no card, known
word or name takes is `learned`) and `properNames` (off by default, since no card stands behind a
name; on, a name or Latin text is `proper`, blue; off, it is still read whole, so no deck word is
found inside it, and drawn plain, save a katakana head `katakanaKnown` still makes `learned`). Like the names, what they add is a status (a known word keeps
its card's pitch, the rest have none), so it shows only with `cardStatus` on; it needs no deck:
until a deck answer builds the index, a failed ask (no card mined and no deck chosen, Anki closed
or refusing) builds one of the known list alone, and the names, known words, particles and
katakana words are drawn from that (`lookOf()` draws plain text only before the first answer, see
"The content side"). `addon/words.js`
(`SHISUKO_WORDS`, a plain frozen object like `SHISUKO_MATCH`, loaded between `match.js` and the
two scripts in both `background.scripts` and `content_scripts[0].js`, and by `service-worker.js`)
is shared: the background turns one deck's notes into `[word, status, pitch]` entries (a word
usually written in kana twice, under its reading as well), the content script builds an index
from them and marks the words of every line. Everything in words.js is pure, without DOM.

### words.js

- Fields. `plainText(html)` strips `<rt>`/`<rp>` with their content, turns `<br>` and the end of
  `p`/`div`/`li`/`tr` into line breaks, strips every other tag (`TAGS` = `/<[^<>]*>/`: a tag never
  runs across a `<`, so an unclosed `<` costs its length, not its square), decodes the six
  entities of `match.js` and collapses whitespace. `plainWord(html)` takes the first non-empty
  line, without bracket furigana (` 食[た]べる` -> `食べる`). `readingOf(html)` is the `<rt>` texts
  of a ruby, the brackets of the furigana or the field itself, and `""` unless what is left is
  kana (ー and ・ allowed). Bounds, since a field is third-party content: `stripMarkup()` and
  `parsePitch()` read at most `MAX_FIELD_HTML_LEN` (16,000) characters of the raw value,
  `plainWord()` at most `MAX_WORD_FIELD_LEN` (320) of a line (the index drops words over
  `MAX_WORD_LEN`, 40), `readingOf()` answers `""` for a raw field over `MAX_READING_FIELD_LEN`
  (1,000). `moraCount(kana)` counts every kana and ー, not the small ゃゅょぁぃぅぇぉヮゎ.
- `parsePitch(text, reading, word)` -> category or null, in this order: a category name in the
  plain text, the first by position (`heiban|平板|atamadaka|頭高|nakadaka|中高|odaka|尾高`,
  case-insensitive: Yomitan's `{pitch-accent-categories}`; its `kifuku` for verbs and adjectives
  is no category, so no overbar rather than a wrong one); else a drawn pattern (`drawnPitch()`,
  per `<li>` of a list, the first that draws one counting): the `{pitch-accents}` markup (one
  `display:inline-block` span per mora holding a `border-color:` line span, the drop after the
  mora whose line has `border-right-width`; a nasal mora's extra inline-block span is not a mora,
  the line spans are counted), else the `{pitch-accent-graphs}` SVG (`graphPitch()`, the first
  `<svg>`: the `<circle>`s of radius 10 or more are the moras in document order, the radius-5 dot
  inside the hollow one is not; the drop after the first circle whose style or attribute says
  `fill:none`, none hollow is heiban; the particle's triangle `<path>` is not read; Jidoujisho's
  graph, dots of radius 4 and 5, reads nothing); else a position `n`: the first `[n]` of the plain
  text (`{pitch-accent-positions}`, a list's first), else a plain text of digits alone, else `ꜜ`
  in the plain text (n = the moras before it); else null. A kana-only value without a mark is null
  (it used to be heiban: the viewer's oldest 302 cards hold the bare reading in the pitch field
  whatever the pattern). `categoryOf(n, m)`: 0 -> heiban, 1 -> atamadaka, else `n === m` ->
  odaka, otherwise nakadaka, with `m` the drawn mora count, else `moraCount()` of the pitch text
  without ꜜ when kana-only, else of `reading`, else of `word`, else unknown (nakadaka).
- `pitchOf(fields, settings)`: `fields` is `notesInfo`'s `{name: {value, order}}`. The candidates
  are the field `ankiPitchField` names (trimmed, in any case, `fieldKey()` as in `mining.md`, when
  present), else every field whose name
  matches `/pitch|accent|アクセント/i` in `order`; the first whose value `parsePitch()` reads
  decides (a field drawing nothing this reads, such as Jidoujisho's graph, before a position field
  does not hide it). The reading is the lowest-order field other than the candidate matching
  `/reading|furigana|読み|よみ/i` and not `/sentence|文/i` (`isReadingField()`; `SentenceFurigana`
  holds the sentence's kana, whose mora count would make every odaka word nakadaka), through
  `readingOf()`; the word is `ankiWordField` (in any case), else order 0, through `plainWord()`. When no
  candidate reads, or there is none, the reading fields are the last resort, in order, through
  `drawnPitch()` alone: Yomitan's `{pitch-accents}` is often the reading field itself, while a
  plain reading draws nothing. Null otherwise.
- `usuallyKana(fields)`: true when a field's value holds `usually written using kana alone`
  (Jitendex's tag title, JMdict's note; case-insensitive, the whole value scanned, since a
  glossary runs past `MAX_FIELD_HTML_LEN`) or a parenthesised list with an item that is exactly
  `uk` (`TAG_LIST`, up to 200 characters to the next parenthesis of either kind, so
  `(adv, uk, JMdict (English))` counts and `(UK)`, `(uk-based)` do not). Linear on a field of
  `(` or `<`. `kanaReadingOf(fields, settings)`: the reading `pitchOf()` would take
  (`readingFrom()`, the one helper both use), the word field and the pitch fields skipped; `""`
  unless `usuallyKana()`, the word holds a kanji, and the reading differs from it and fits
  `MAX_WORD_LEN`. Measured: every note's reading indexed made half the new hits false (勝手 in
  向かって, 内容 in 出さないように); the tag filter kept 35 hits, all right.
- `statusOf(sets, noteId)` over the sets of the five searches below: not in `unsuspended` ->
  `suspended` when in `suspended`, else null (not in the deck); in `new` -> `new`; in `learning`
  -> `learning`; in `review` -> `learned`; else `learning` (an Anki before 2.1.44, or a set a
  search left out). `mergeStatus(a, b)` for two notes of one word: the least progress wins
  (`new` < `learning` < `learned`), `suspended` only when both are, null the identity.
  `STATUSES` ends in `proper`, the colour of a name (below), which no card has: `mergeStatus()`
  lets any card's status beat it.
- `buildIndex(entries, known)`: words trimmed; empty, over `MAX_WORD_LEN` or in `PARTICLES`
  dropped (case, binding, adverbial, conjunctive and sentence-final particles, Yomitan's fusions
  such as のは, への, かも, the んじゃ ICU keeps whole, and the copula and auxiliaries a learner
  mines: だ, です, ます, ない, たい, ん, じゃ, もん …: a card for one would paint every line);
  duplicates merged (`mergeStatus`, the first non-null pitch). `known`, the viewer's known words
  (an array of strings, the same rules), are `learned` over whatever their cards say, the card's
  pitch kept, and one without a card is an entry of its own with its stems, so its forms are
  found like a deck word's; a kana reading's entry (`kanaReadingOf()`) is overridden like any.
  Returns a frozen `{size, exact, stems, heads, maxLen, maxStemLen}`:
  `exact` Map word -> `{word, status, pitch, bounded}` (`bounded` for a word without kanji or
  katakana, which must end at a word boundary in every form, else ある is found in あるいは);
  `stems` Map stem -> `[{entry, kind}]` from `stemOf()`: a stem of at least one character for a
  word holding a kanji or katakana, of at least two for a kana-only word (かける -> かけ, しまう
  -> しま, おいしい -> おいし; いう, する, くる, みる, いい stay exact: a stem of one kana would be
  found in every line): `する` beyond the stem -> kind `suru` (こする, with two kana to spare, is
  a る verb); else a last `い` -> `i-adj`, `る` -> `ru` (ichidan or godan, unknown), one of
  うくぐすつぬぶむ -> that kana; anything else has no stem, and neither has a kana-only word
  whose stem is itself in `PARTICLES` (から of からい, なら of ならう, しか of しかる): it matches
  exactly only, since からかった would else be からい's かっ + た (the kanji twin 辛い keeps its
  stem). `heads` is the set of first characters, so a position whose character starts no word
  costs nothing.
- `wordStarts(text)`: the indices where a word may begin, `Intl.Segmenter("ja", {granularity:
  "word"})` segment starts (the instance cached) plus 0; every index without a segmenter or on
  any error; it never throws. ICU keeps a compound in one segment (日本語, あるいは, 見せかけ).
- `markWords(text, index, starts, opts)` -> runs `[{text, status, pitch}]` covering the text in
  order, unmatched characters joined into one run with nulls; `opts.particles` and
  `opts.katakana` count the particles and the katakana words as known and `opts.names` draws the
  names `proper` (below); without `opts` the deck and the known words are all it colours, a name
  being taken whole and left plain. Left to right; only a
  position in `starts` (an iterable, `wordStarts(text)` by default) is tried, a deck word only
  where the character is in `heads` (a null or empty index finds none, the names and options
  still apply); after a run `i` jumps to its end (no overlaps). At a position, in this order: a
  deck or known word (with its honorific prefix or quotative, below) unless a name from the same
  start is longer, a name, a katakana word (`opts.katakana`), a particle (`opts.particles`). The
  boundaries it goes by are `boundsOf(text,
  starts)`: `starts` plus the index after a て or で at a start that ICU fused with the く of the
  auxiliary behind it (`TE_FORM`: 食|べ|てく|れ|た, 書|い|てく|れ|た, かけ|てく|れ|た; not
  読|んで|く|れ|た, which it cuts), where くれる begins and かけて ends; `starts` itself is never
  written (the content script keeps it per cue), the set is copied when there is something to
  add. `matchAt()` takes the longest span, the exact word on
  a tie, and a kana-only (`bounded`) exact word over a form of itself that adds particles alone
  (`particlesOnly()`: a walk over `particleShapes()`, the entries of `PARTICLES` that end at a
  word boundary, from the word's end to the form's), so a kana noun ending in a verb's kana
  (いくつ, きょう, けっこう, ふつう, ほんとう, whose stem is in the tables) ends before its copula
  and carries no です in its pitch overbar (いくつ|です|か), a kana verb losing nothing by it
  (わかる|んだ, おいしい|です: the copula is no part of it either way), while a form that adds
  more than particles still wins (わかりました) and a kanji word keeps the form (食べるでしょう is
  one run): exact words longest first (`bounded` ones must end at a boundary or the end of the
  text, the others anywhere `endsWord()` admits: the end, a boundary, or not right before a
  kanji, katakana or ー, so 関 is not coloured in 関係, 飲み not in 飲み物, while 見た ends before
  犬 and 電話 before 番号, and never inside a segment that is a particle, `insideParticle()`:
  電話|しか has no form 電話し, unless `NOT_BEFORE` makes the particle another word's start,
  食べ|たら|しく); an exact word `suruNoun()` admits then runs on over する's pieces and tails,
  `continuationEnd(..., "suru")` (勉強している, お願いします, スタートしました with スタート known,
  no card for 勉強する needed): a word holding a kanji or katakana, of two characters or more
  (顔する, 数する stay two runs, and so do 損する and 得する, the few single-kanji する nouns), not
  ending in する (its own forms, below), not in `NOT_SURU` (the time words, adverbs, pronouns and
  counters a する form follows as often as any noun: 明日, 最近, 一番, 一回, 全然, 絶対, 結構,
  多分, 本当, 少し, 自分, 皆さん …; a noun ending in し is one, 引っ越しします, so 少し is listed
  by name), not opening on 何, 毎 or 今 (`NOT_SURU_HEAD`: 何してるの, 毎日します, 今日します) and
  not ending in か, も, で, に, と or は (`NOT_SURU_END`: 何かしたい, 何もしない, 後でします,
  静かにして). The run must reach past a lone し, which is the conjunctive particle as often
  (勉強し、 is 勉強 alone), and past every form a deck word found below reaches from the same
  start, so a card for the verb keeps its own forms when they are as long (話して is 話す's with
  話 and 話す in the deck, 思い出して 思い出す's; without the verb's card the noun runs on as
  before); and it never ends on ちゃ or じゃ before っ (勉強しちゃった, 渋滞しちゃってる: the
  tables cannot follow ちゃった, and a run ending at ちゃ kept a card for ちゃう from the rest; the
  noun stays alone). Then every stem length from `maxStemLen` down, `continuationEnd()` giving the
  furthest end `endsWord()` admits, or, for a `bounded` entry, one at a boundary or where
  `boundedEnd()` admits: inside the segment ICU made of the form's ending and the particle
  after it (わか|っ|たよ, かけ|たよ, でき|ますよ, ちがい|ますよ: the segment holding the end must
  begin after the form's start, be no particle itself, and what remains of it must be a
  particle), while the exact bounded word, having no ending to be fused, still ends at a boundary
  only (ことば, あいだ, はなし and こんにちは with こと, あい, はな and こんにち in the deck stay
  plain, and so does はし in ICU's は|しか). ICU cuts a kana run it does not know into single
  kana (い|れ|ば, かけ|ら, し|まっ|た), so かけて in 話し|かけ|て|いただく, しまった and わかった are
  found and いれば is not いれる (ば is no first piece). Its rules:
  - The bare stem counts for `suru` (勉強 in 勉強が; a bounded `suru` entry keeps it at a boundary
    too, びっくり in びっくり|だ) and for `ru` when the stem ends in an i-row or e-row kana
    (`IE_ROW`) at a boundary (the ichidan 連用形 is the noun: 食べ in 食べに行く, 助け, 考え, 流れ),
    for `ru` never for a `bounded` entry (かけ in かけ|ら, いれ in い|れ|ば would be nouns at every
    cut); a stem ending in a kanji (走, 見) or the a-row (当た, 変わ: a godan verb, whose noun is
    its り piece, found through the tables) is no form, and nothing ends inside a compound ICU
    holds together (見せ in 見せかけ, 当た in 当たり前).
  - Otherwise a first piece from `FIRST_PIECES[kind]` must follow (`suru`: する し さ せ す すれ;
    `i-adj`: い く かっ けれ さ そう くて くない ければ; `ru`: る た て ない … られ させ よう れば ろ よ
    ず ん ら り れ っ なかっ なけれ, the ichidan stem being the 連用形; the godan rows わいうえおっ,
    かきくけこいっ, がぎぐげごい, さしすせそ, たちつてとっ, なにぬねのん, ばびぶべぼん, まみむめもん),
    so 走 in 走者 is not 走る. 行く's い is skipped (`text[pos - 1] === "行"`): its 音便 is っ alone,
    and 行い, 行います, 行いたい are 行う's; and っ is admitted after 行 alone, since every other く
    verb takes い (歩いた, 書いた), so its っ is another word's: あるって is ある and the quotative
    って, not あるく's, and はたらって, 書って are no forms.
  - `tailEnds()` then consumes up to `MAX_TAILS` (5) pieces of `TAIL_PIECES` (た て で だ ない …
    ます まし ませ ん たい … れる られる せる させる ば う よう ろ る い けれ ず ちゃ じゃ てる でる てい
    でい いる いた いて います いない ましょ でし でしょ です たら だら たり だり ても でも ながら なさい
    まい とく どく いか いき いく いけ いこ いっ いただく いただき いただけ いただい いただこ いただか っ
    ー), every split tried (泳いでいる is で + いる, not でい + る), each piece checked against the
    one before it by `firstRole()` (a godan first piece by its row `a`/`i`/`u`/`e`/`o` or `onbin`,
    a す verb's し as `shi`, the adjective's い as `adj`, a する verb's さ/せ/す as `suru:さ` etc.,
    a る verb's っ as `onbin`, any other piece by its text): `AFTER` lists what a single-kana tail
    and the いる/いく/いただく pieces may follow (だ after ん and not る: 食べるんだ, 食べる + だけ; た
    after 音便, まし, て and the ichidan-like stems, not after い, so た after いる's stem is the
    tail いた; う after the o-row, よ, ろ, ましょ …; よう only after し and the ichidan-like pieces,
    since after る, た, ない, the u-row or an adjective it is 様: 食べる + ように; ん after the forms
    it shortens and not ちゃ: 食べてちゃ + んと); `OPEN_TAILS` are pieces a span never ends right
    after (い, てい, でい, いか, いこ, いっ, いただい …, the a-row and o-row, ら, the 音便 kana,
    suru:さ/せ/す, かっ, なかっ, たかっ, けれ, なけれ, まし, でし: 聞こえる and 死の恐怖 have no run
    for 聞く / 死ぬ, 電話さえ is 電話 + さえ, 行い / 引っかかった have no run for 行く / 引く, 見たかっこいい
    is 見た + かっこいい, 食べるけれど is 食べる + けれど); `NEXT` lists what the 音便 kana may be
    followed by (the た/て pieces, ちゃ, じゃ, とく, どく, たら, たり, ても …: 行います has no run for
    行く); `NOT_BEFORE` keeps a piece from ending a span where the text after it makes it another
    word (ても/でも before ら: 食べて + もらう; たら/だら before しい しく しか しけ しさ: 食べた + らしい;
    たく before せ さ ら: くせに, たくさん, くらい; た before くさん; いき before な: いきなり; いく before
    ら: いくら; いた before だ: いただく, so 食べていただく is followed to its end and never cut inside).
    The furthest valid end wins; the span is at least stem + 1 except for the two bare stems.
  - Known gaps, listed rather than promised: 食べちゃった / 食べちゃって stop at 食べちゃ (っ is no
    tail after ちゃ / じゃ); 食べたがる and 勉強できる are not covered; 来い, 行こ！ (the volitional
    without う before punctuation), 行かねば / 行かぬ, 書いといて / 読んどいて (とく is a tail, とい is
    not, and 書い alone is no form) and 書きそう / 話しそう (そう is a first piece of the ichidan and
    adjective tables, not a tail) are not matched; 〜てもらう is 食べて + もらう (もらう is no tail,
    unlike いただく). Of the kana verbs: はいた (はいる, but also 吐いた and 履いた) and わかれ (わかる,
    but also 別れ) are homographs the matcher colours; からいよ (ICU から|いよ: the exact word at
    no boundary) and the くれた of 見てくれた (ICU's noun 見てくれ) stay plain; かけたらしい and
    かけたくさん (ICU かけ|たらしい, かけ|たくさん) stay plain, since らしい and くさん are no
    particles and `NOT_BEFORE` keeps た from ending the form before them.
  - It stays linear-ish: Maps keyed by the substring, never a loop over the deck per position.
- A particle never takes the colour of the word before it (the one exception is the quotative
  before いう without the particles option, below): 領域まで and 領域の with 領域 in the
  deck read `[領域]まで` and `[領域]の`, not one red piece (the colour says "this word's card is
  new", and まで has no card). ちょうどこのお風呂の中で with 風呂 and 中 in the deck is
  `[ちょうどこの][お風呂][の][中][で]` without the particles option. What takes a colour without
  being a deck word is a run of its own; content.js draws every run, and adjacent runs of one
  status stay separate. A word with a pitch and no status has no colour to run on: what would
  take its colour is plain text.
  - An honorific prefix (`HONORIFICS`: お, ご) joins the word it fronts: at a start `i` whose
    character is one AND `starts.has(i + 1)` (ICU cut the prefix off: お|風呂, ご|家族, お|仕事; it
    keeps お茶, お前, お金, ご飯, お母さん whole, so those are never tried and 前 never colours
    お前), when no word matches at `i`, the match is tried at `i + 1`; on a hit the prefix's run,
    the word's status and no pitch, comes before the word's.
  - A quotative (`QUOTATIVES`: って, と, longest first) may front いう, and いう alone
    (`QUOTED_WORD`), inside one segment: ICU keeps っていう and という (彼|という|人) whole, so
    いう never begins a segment, while it cuts って off every other word (って|こと, って|もの)
    and keeps ところ, とおる, とまる and とくに whole because they are words of their own, so
    those stay plain with ころ, おる, まる or くに in the deck. At a start `i` holding one, when
    no word matches at `i`, the match is tried at `i + prefix.length` and taken only when it is
    いう and ends at a boundary or the text end, so いう is found in っていう / という and not in
    そういう or といった. With the particles option the quotative is a particle, `learned`;
    without it, it takes the status of the run that ends at `i` (って after 話しかけていただく, と
    after 猫), else it stays plain (彼と | いう, and after a name: blue is no card's colour).
  - Names, status `proper` (blue in content.css) with `opts.names`; without it the same span is
    taken whole, so no deck word is found inside it (東京 with 京 in the deck stays plain) and the
    particle rule never takes it over, and joins the plain text around it, save its katakana head
    (`katakanaAt()` from the name's start, cut at the name's end), which `opts.katakana` still
    makes `learned`: hiding the names must not take back what the katakana switch asked for
    (アメリカ green, スカイツリー of スカイツリー駅 green and 駅 plain). Found where no deck or known word from the same start is
    as long (`nameAt()`; a card wins the tie, so 東京 alone is the card's, while 東京駅 and 丸の内
    are blue with 東京, 駅 and 丸 in the viewer's deck, whose の of 丸の内 was left white by "the
    deck always wins"): Latin text (`LATIN`, sticky: a letter, then letters, digits, `'`, `&`, `.`
    and `-`, ending in a letter or digit, or the full-width twin with its own ’ ＇ ＆ ． and －
    inside, which ICU cuts apart: jr, YouTube, iPhone, E4, ＪＲ, Ｗｉ－Ｆｉ, Ｑ＆Ａ; digits alone are a
    number; a single letter takes the katakana segment it is fused to, `latinEnd()` and
    `KATAKANA_WORD`, so Tシャツ and Jリーグ, cut T|シャツ, are one name; laughter, `w` or `ｗ` alone
    however many, `LAUGHTER`, is none: www, ｗｗｗ, 草www); a place (`PLACES`, 406 of them: the
    prefectures with and without their suffix, 大分 bare left out for だいぶ, their capitals but 津
    (Mie's, not in the list), the cities over half a million, the 23 wards with 区 and the
    unambiguous ones bare, the districts and sights a travel video names, 日光, 両国, 新世界 and 高山
    left out as common nouns, the regions, Japan and the countries and cities abroad that come up,
    チリ left out as the chili of エビ|チリ and チリ|ソース; longest first, `placeAt()`, ending where
    `endsWord()` admits, so 日本 is not in 日本語); and a place, or, where no deck or known word is
    found at the same start (`nameAt()`'s `pair`), a kanji or katakana segment of two characters or
    more (`KANJI_OR_KATAKANA_RUN`), with a `PLACE_SUFFIXES` segment after it (駅 県 市 区 町 村 郡
    都 府 島 山 川 湖 海 港 寺 城 橋 線 湾 峠 岬 滝 岳 空港 公園 神社 通り 温泉 半島 高原 海岸 商店街
    タワー ドーム) as one run (`suffixEnd()`: the rest of the segment the name ends in, 東京駅 kept
    whole, or the segment after it, 品川|駅; 駅前 and 駅舎 are words and take none: 東京|駅前,
    丸の内|駅舎). The pair of a segment and a suffix is a guess, and a card for the segment says
    otherwise: 結構|山, 昨日|海, 地元|駅 and 天然|温泉 are a word and a noun, each in its own
    card's colour, while 主要駅 without a card for 主要 is a name. ICU cuts 東|急|線, so 東急線 is
    no name (a known gap), and an ordinary word of two kanji without a card before 通り is one
    (予定通り, 時間通り).
  - With `opts.katakana`, a katakana word (`KATAKANA_RUN`: two characters or more, ー and ・
    inside, never one) that no deck word, known word or name takes is `learned`, cut before a
    deck word that begins inside it at a boundary (`katakanaAt()`: コーヒー|カップ).
  - With `opts.particles` (the setting `particlesKnown`, off by default: on, a particle counts as grammar
    the viewer knows, another claim than the one 0.12.0 removed, where a particle took the card
    state of the word before it), at a start where nothing above matched, `particleAt()`: the
    quotative with いう ICU keeps whole (`QUOTE_PHRASES`: っていう, ていう, という; という only
    where no card holds いう or という and っていう only where none holds いう, since the quotative
    rule above, tried first, fronts いう with って or と, and a card for という is a word of its
    own; ていう whatever the deck says, since no quotative fronts いう with て, so a card for いう
    does not show in こと|ていう|か), else the longest entry of `PARTICLES` that ends at a boundary,
    is `learned`, one run each (本|には|ね, です|ね), never the colour of the word before it; else
    a one-kana particle ICU fused with the い of いる after it (`IRU_PARTICLES`: が, と, も, に,
    で; 人|がい|た, 猫|とい|た, 猫|がい|れ|ば), that い followed by a boundary and one of
    `IRU_ENDINGS` (た て ない なかっ なく ます まし ませ る れば よう たい), never at the end of the
    text (猫|がい); not は (靴|を|はい|た is 履いた: はいた, はいて and はいる are 履く, 吐く and 入る
    in kana as often, and ICU fuses them the same way), nor と after the 音便 kana い, っ or ん
    (`ONBIN_KANA`: 置い|とい|て is ておいて). ICU cuts a kana word it does not know into pieces
    whose first often has a particle's shape (や|って, な|っ|た, お待ち|し|て, も|ら|って), and the
    auxiliaries after a verb stem the deck lacks are its inflection (ござい|ます, あっ|て), so
    `shreddedVerb()` refuses a piece, a shorter particle standing where a longer one is refused
    (に of 猫|に|も|ら|っ|た):
    - a one-kana particle before っ, the 音便 of the verb it begins (やった, なって, よかった,
      つながった, かかった; not だ, whose past is だった, nor the の of 見るのって, nor ね or よ
      before a quotative って of its own that ends at a boundary, 楽しい|ね|って, while
      すごい|な|って stays plain, なる's as often), or before a た or て that ICU left alone or
      fused with a particle only (`pastAlone()`: 猫|が|で|た, 猫|が|で|たよ; 出る, 寝る, 似る);
    - a piece `PARTICLE_NOT_BEFORE` names before the kana after it: し before て, ま, ち and な
      (する) and before れ and ろ (しれない, しろ); も, にも, でも, とも and ても before ら (もらう);
      か before か (かかる); や before り and れ (やる) and ば (やばい); よ before か (よい); へ
      before え (へえ); っけ before え (でっけえ);
    - な before the whole segments に, さ and さそう (`PARTICLE_NOT_BEFORE_SEGMENT`: な|に is 何,
      な|さ and な|さそう are ない's, 情け|な|さ, 申し訳|な|さそう; whole segments, so 変|な|におい
      and 好き|な|さかな keep their な), and the second half of such a pair after plain text (the に
      of な|に, the さ of 情け|な|さ);
    - a one-kana particle, or っけ, before a small kana that glides onto the kana before it
      (`GLIDES`: ゃ ゅ ょ ャ ュ ョ ゎ ヮ; おっ|し|ゃ; not the small vowels, which draw a particle
      out, だ|よ|ぉ), and a one-kana particle before a single-kana segment that is no particle with
      a small kana after it (だ|せ|ぇ: slang ICU does not know);
    - after a kanji, the kana a verb the deck lacks is cut into: ん (飲|ん|だ, and the ん of
      僕|ん|家 with it), a piece opening on だ or で after that ん (飲|ん|だ, 死|ん|で,
      飲|ん|だら), and the a-row okurigana さ, わ, ば, な and か (`A_ROW_OKURIGANA`) before the
      negative, the passive or the causative (`NEGATIVE_HEAD`: な, ず, れ, せ; 書|か|ない,
      話|さ|ない, 言|わ|ない, 呼|ば|れ|た, 移|さ|れる, 行|か|せ|て), except after 何 and 誰, where
      the か is the particle (何|か|ない);
    - って, the て-form of an う, つ or る verb, after an a-row kana (`A_ROW_BEFORE_TE`:
      あかさなはまやらわがざばぱ and ゃ) where plain text ends (な|って, や|って, もら|って,
      強ま|って; not とか|って, where a particle ends, nor after a deck word, さくら|って), and
      after ちゃ or じゃ whatever ends there (使っちゃ|って, where the form of a deck verb stops, as
      the tables do); た|って and から|って stand;
    - an entry of `INFLECTIONS` (て ば たら たり ても ながら ます ません ました ない たい) right
      after plain hiragana (ござい|ます, やり|たい; after a particle, a word, a kanji or punctuation
      it stands: お金が|ない, んじゃ|ない, 問題|ない).

    Measured on 668 of the viewer's subtitle lines with their 13,730 deck entries, known list
    empty, katakana off and Node 24's ICU: 2,198 particle runs unguarded, 18 of a sample of 200
    judged by hand no particle (91% right; 94% counting a verb's inflection as a particle); the
    guard as it stood then refused 206 (196 no particle; the ten were the Kansai copula や before
    った/って, 日本初やった, and ない after an unfound word, もちろん|ない, もんじゃ|ない) and the
    quotative phrase and んじゃ added 34, all right, for 2,026 runs, of which two samples judged by
    hand (188 and 142) were 96.8% and 96.5% right. The rules added after those samples (the glides,
    the second kana of a slang word, な|に, the kana a kanji verb is cut into) took 8 more of the
    2,026 off those lines, and 25 of 2,237 with an empty deck, where no card holds the verb: none
    of them a particle. The rule that comes to mind first, a one-kana particle before any
    single-kana segment that is no particle, refused 25 particles with its 33 pieces (を|お|ご|ら,
    に|い|ます, で|ご|ざ|い: ICU cuts the word after a true particle the same way), hence the small
    kana it now asks for after that segment. Left, listed rather than promised: shapes too rare to
    list (つ|な|が|っ|た, な|げー, と|ろ, へ|ん, な|け|れ|ば), words the cue boundary cut (さ|に of
    まさに), a katakana word used as a verb stem (シュイ|ません); a verb that no deck holds cut into
    particle shapes of two kana, a kana one (わ|から|ない) or a kanji one's okurigana (分|から|ない:
    から after a kanji is the particle as often, 朝|から|ない), stays green; the Kansai copula や
    before った and って stays plain; and a deck verb's form that ends inside a particle ICU fused
    to its ending stays plain whole (読|ん|だって with 読む in the deck: 読んだ would end inside
    だって, and ん and だって are the verb's to the guard).
  - With `opts.particles`, at the same start, `grammarAt()`: a grammar word (`GRAMMAR_WORDS`, its
    own small index built once: the verbs that carry the grammar, ある いる おる みる する くる なる
    いく おく やる しまう くれる もらう あげる, in their common forms, since a verb whose stem is one
    kana matches its exact form only; そう よう みたい らしい; the こそあど words; the formal nouns
    こと もの ため わけ はず ところ とき ほう; まだ もう また よく もっと ずっと ちょっと and the like)
    is `learned`, the longer of it and the particle winning. A deck word at that start still wins
    (a card says more). Being bounded kana words, they never fire inside a word ICU keeps whole
    (いただきます, ありがとう, したがって, あるいは, いくら); they are refused after a single kanji
    or kana of plain text, whose okurigana they would be (書|い|た, 思|い|ます, ご|ざ|い|ます), and
    なっ after い or だ is the sentence-final な before a quotative (すごいなって), never なる.
  - The copula's seams (`copulaSeams()`, added to the bounds by `boundsOf()`), which Firefox's
    segmenter (ICU4X) hides where Node's ICU does not, or the other way round: after そう, よう or
    みたい fused with particles only (`COPULA_HEADS`: 良さ|そうだ|な, そうだね is そう|だ|ね), and
    after the copula's second half when ICU cut a lone だ or で off it and fused the rest with
    particles only (`COPULA_CUT`: 大変|だ|ったね, 学生|で|したね, Node's 感じ|だ|っ|たん). What
    follows must split into particles alone (`particleSplit()`), so no word ICU knows is cut
    (そうじ, ようやく). The adjective's form runs through the copula's past after そう (the tail
    `だっ`: 楽|しそう|だ|った is 楽しそうだった whole).
  - `followOn()`: with the option, where a run ends inside a segment (a deck word boundedEnd() let
    end before particles, 広さ in Firefox's 広|さも|ある; a particle fused with the い of いる,
    人|がい|た), the particle or grammar word there is `learned` too.
- Tests: `addon/tests/words.test.js`.

### The background index (`addon/background.js`, "word colours" section)

- `deckSearch(name)` -> `"deck:NAME"` with `\`, `"`, `*`, `_` backslash-escaped inside the quotes
  (Anki's syntax; the deck and its subdecks). `deckScope(url, deck)`: the names `current` and
  `filtered` are keywords to Anki's search with no escape, so those two are searched as
  `did:<id,...>` (the deck's own id and its subdecks' from `deckNamesAndIds`), null when no such
  deck exists (an empty deck, like any other missing name: AnkiConnect answers `[]`; the popup's
  deck list is the guard).
- `fetchDeckIndex(url, deck, settings, signal)`: the five `findNotes` searches (`STATUS_QUERIES`,
  each `${scope} <clause>`): `suspended` `is:suspended`, `unsuspended` `-is:suspended`, `new`
  `is:new -is:suspended`, `learning` `is:learn -is:suspended`, `review` `is:review -is:learn
  -is:suspended` (since Anki 2.1.44 the three go by the card's type, which a suspended card keeps).
  The deck's ids are suspended ∪ unsuspended, sorted ascending: Anki's `findNotes` has no ORDER
  BY (a review that moves a due reorders its answer), and the same deck must give the same
  entries or every tab would take them in again. `notesInfo` (chunks of `NOTES_INFO_CHUNK`, 200,
  `CARD_STATUS_TIMEOUT_MS` 20 s per request) runs only for ids not in the note cache (`notes:
  Map<id, {word, reading, pitch, mod}>`, kept across refreshes and mines, ids gone from the deck dropped)
  and for known notes edited since `checkedAt`: a sixth search `${scope} edited:<days>` (days =
  `max(2, ceil(elapsed / day) + 1)`; an Anki without it finds nothing) lists candidates and
  `notesModTime` re-reads only those whose `mod` moved (without the action, every candidate).
  Word = `SHISUKO_WORDS.plainWord(noteSummary(info, settings).word)` (`ankiWordField`, else
  order 0; over `MAX_WORD_LEN` stored as `""`), pitch = `SHISUKO_WORDS.pitchOf(info.fields,
  settings)`, reading = `SHISUKO_WORDS.kanaReadingOf(info.fields, settings)`. Entries: one
  `[word, status, pitch]` per note with a word and a non-null `statusOf(sets, id)`, followed by
  `[reading, status, pitch]` when the reading is not empty, by ascending id. A `notesInfo` chunk that fails keeps what `known` said
  for its ids (a re-read note keeps its colour and old pitch) and is asked again on the next
  refresh; a `TypeError` or `AbortError` fails the ask as a whole. `dropped()` (the `signal`) is
  checked before every request and once more after the loop, so a fetch dropped during its last
  request answers nobody. Returns `{deck, wordField, pitchField, at, fetchedAt, checkedAt,
  entries, notes, changed}`: `at` is kept from the previous index when the entries came out the
  same (`sameEntries`), so a tab holding them hears "unchanged"; `fetchedAt` is the clock the
  TTL runs on; `changed` = a note read (`read > 0`) or dropped (`notes.size !== known.size`) or
  the record restored, and decides whether the session record is written.
- The cache: `cardIndex = {deck, wordField, pitchField, at, fetchedAt, checkedAt, entries,
  notes}` and the fetch under way `cardIndexInFlight = {deck, wordField, pitchField, promise,
  controller, expired}`, both named by the deck and the trimmed `ankiWordField` /
  `ankiPitchField` they were read with; `indexFor(index, deck, settings)` compares all three and
  is what the fresh and stale answers, the flight joining and `fetchDeckIndex`'s `previous` go
  by. `refreshCardIndex(url, deck, settings)` joins a flight for the same deck and fields and
  aborts one for anything else through its `AbortController` before starting anew (the walk ends
  at its next request with an error marked `dropped`; the waiting `cardStatus` re-runs once for
  what is set now). `dropCardIndex()` (the `browser.storage.onChanged` listener, when one of
  `CARD_INDEX_SETTINGS` = `cardStatusDeck`, `ankiPitchField`, `ankiWordField` changed; `ankiUrl`
  is not among them) forgets the index, moves `cardIndexGeneration` on, aborts the flight and
  clears the session record. `expireCardIndex()` sets `fetchedAt = 0` and marks a flight
  `expired` (its index lands expired), the notes staying. The notes also live in
  `storage.session` under `DECK_NOTES_KEY` (`deckNotes` = `{format, deck, wordField, pitchField,
  at, checkedAt, entries, notes: [[id, word, pitch, mod, reading]]}`, `notesRecord()`), so a
  return to YouTube after the event page ended does not read the whole deck again:
  `restoreNotes(deck, settings)` ignores a record whose `format` is not `DECK_NOTES_FORMAT` (2:
  a record without readings would keep them out until each note is edited), a record for another
  deck or other fields, and hands back `at` and `entries` only with a
  positive `at` (0 is what a tab holding nothing sends), so the stamp survives a restart when the
  entries came out the same; the page that restored it writes it once more.
- The deck: `rememberDeck(url, noteId)`, from `addToAnki()` after every card it filled, not
  awaited: `findCards {query: "nid:<id>"}` -> `getDecks {cards}` -> the deck with most of them ->
  `storage.local` `DECK_SEEN_KEY` (`ankiDeckSeen` = `{deck, at, noteId}`) and `expireCardIndex()`,
  so the new card shows red at the next ask rather than after the TTL; errors are `console.debug`.
  `seenDeck()` reads the record; `resolveDeck(settings)` -> `{deck, automatic}`: the trimmed
  `cardStatusDeck`, else the seen deck (`automatic: true`), else `{deck: null, automatic: true}`.
- `cardStatus(msg, retried)`, the handler of `{type: "cardStatus", since?}`: captures
  `cardIndexGeneration`, then `getSettings()`; not `wordColoursOn(settings)` (`cardStatus ||
  pitchAccent`) -> `{ok: false, reason: "off"}`; no deck -> `{ok: false, reason: "noDeck",
  error: "No card mined yet; pick a deck in the popup"}`; `ankiPermission(url)` false -> `{ok:
  false, reason: "denied", error}` (the shared helper: one dialog at a time, see the mining
  section); the generation moved while the dialog was up -> once more for the settings of now;
  an index for this deck and fields with `fetchedAt` under `CARD_STATUS_TTL_MS` (30 s) is
  answered from memory, else `refreshCardIndex()`. The answer is `{ok: true, unchanged: true,
  at, deck}` when `msg.since === at`, else `{ok: true, deck, automatic, at, entries}`. On a
  failure the old index is answered with `stale: true` when it has entries, else `{ok: false,
  reason: "offline", error: "Anki is not running or AnkiConnect is not installed"}` for a
  `TypeError` or `reason: "error"` with the message; a `dropped` error re-runs once.
- `ankiDecks()`, the popup's `{type: "ankiDecks"}`: `{ok: true, decks: string[] (from
  `deckNames`, sorted), seen: string | null}` or `{ok: false, reason: "offline" | "denied" |
  "error", error, seen}`, through `ankiPermission()` as well. The message switch routes both.
- Tests: `addon/tests/background.test.js` (a fake AnkiConnect answering by `query`).
  `_loadBackground.js` loads `words.js` between `match.js` and `background.js` and exposes
  `CARD_STATUS_TTL_MS`, `DECK_SEEN_KEY`, `DECK_NOTES_KEY` and `DECK_NOTES_FORMAT`.

### The content side (`addon/content.js`, "word colours" section)

- `wordColoursOn()` = `enabled && (cardStatus || pitchAccent)`. State: `wordIndex` (a
  `buildIndex(entries, knownList(settings))` result), `wordEntries` (the entries of the deck answer
  it was built from, kept so that a new known list builds it again without an ask),
  `wordIndexSerial` (moves on with every index put in `wordIndex`, a new one or none: what dates a
  cue's look), `wordIndexAt` (the background's `at`, the `since` of the next ask), `wordIndexKey`
  (`JSON.stringify(entries)`), `wordIndexAskedAt`, `wordIndexInFlight`, `wordIndexGeneration` (times
  the index was started over), `wordIndexDrawn` / `wordIndexDrawnSerial` (the index the last
  `refreshWordMarks()` ran with), and three WeakMaps: `lineTexts` (transcript line -> its
  `.shisuko-linetext` span), `drawnKeys` (element -> the `drawKey()` of what `renderText()` last
  drew in it), `cueLooks` (cue -> `{serial, cardStatus, pitchAccent, katakana, particles, starts,
  runs, key}`). Constants `WORD_INDEX_REFRESH_MS` 30 s, `WORD_INDEX_LOG_MS` 60 s,
  `WORD_INDEX_MINE_DELAY_MS` 1.5 s, `WORD_INDEX_PROBE_MAX` 64, `WORD_SETTINGS` = `cardStatus`,
  `pitchAccent`, `cardStatusDeck`, `ankiPitchField`, `ankiWordField` (if the background's
  `CARD_INDEX_SETTINGS` ever gains `ankiUrl`, add it here too).
- `renderText(el, cue)` is the one writer of a cue's text, used by `setSubtitle()` for
  `state.subText` and by `transcriptLine()` for the line's span: `lookOf(cue)` gives the runs,
  `drawRuns()` puts them in a `DocumentFragment` and `replaceChildren()`s it: a string run is a text
  node, a marked run `<span class="shisuko-word">` with `dataset.status` only when `cardStatus` is
  on and the run has a status, `dataset.pitch` only when `pitchAccent` is on and it has a pitch, and
  its text as a text node; a run with neither is joined into the plain text around it. Without
  `wordColoursOn()` or an index, `el.textContent = cue.text`, so the names, the particles, the known
  words and the katakana words wait for the poll's first answer (a deck's, or a failure that builds
  the index of the known list alone, below), and, being statuses (a known word's card pitch
  aside), show only with `cardStatus` on. `lookOf()` answers from `cueLooks` when the entry's
  `serial` is `wordIndexSerial` under the same pair of colours and the same switches
  (`sameSwitches()`: `katakanaKnown`, `particlesKnown`, `properNames`), else runs
  `SHISUKO_WORDS.markWords(cue.text, index, starts, {katakana, particles, names})` with the entry's
  `starts` (`wordStarts(cue.text)` the first time, kept whatever the index) and records it under the
  serial of now; a look never holds an index, so a cue drawn while the transcript was hidden pins no
  old index. A rebuilt panel and the line on screen for a cue whose line is up ask the matcher and
  the segmenter nothing.
- `refreshWordMarks()`: with the last drawn index and the new one both held and the colours on,
  `indexProbes(prev, next)` lists the words the two disagree on (present in one only, or another
  status or pitch), each replaced by its stem where it has one (a prefix of the word and of every
  form), `[]` when they agree, null over `WORD_INDEX_PROBE_MAX`. The active cue's text and, when the
  transcript is shown, every line are left alone when `sameLook(el, cue, prevSerial, probes)` holds
  (the look carries `prevSerial` with the colours and switches of now, `drawnKeys.get(el)` is its
  key, and the text includes no probe; the look's serial then moves on) and otherwise go through
  `refreshText()` (a redraw only when the `drawKey()` differs), so a card reviewed in Anki costs a
  look at each line's text and a match of the lines holding that word, and replaces neither the
  nodes of any other line nor what Yomitan holds on them. Lines still pending (none, as a rule: a
  shown panel renders on arrival) are put in first by `renderTranscript()`, drawn under the index of
  now; the panel is never rebuilt for a refresh.
- `pollWordIndex()` runs from `syncTick()` and asks only when `wordColoursOn()`, `state.video &&
  state.videoId` (the home page and a player left behind off a watch page never ask, a settings
  change included), the tab visible, nothing in flight and `WORD_INDEX_REFRESH_MS` past: `{type:
  "cardStatus", since: wordIndexAt}`. A tab standing by for another (`state.standby`, see "One tab at a time" in `mining.md`) asks like any other: the index is the deck's and the election is about the
  server, which this never reaches, and the standby tab's lines stay on screen and must be
  recoloured too. An answer from before `wordIndexGeneration` moved is thrown
  away. `unchanged` keeps everything; a new `at` with the same key moves the stamp only; new
  entries go into `wordEntries`, rebuild the index with the known list of now, move the serial
  and call `refreshWordMarks()`; `reason: "off"` drops
  the index and takes the colours off; any other failure (`noDeck`, `offline`, `denied`, `error`)
  is a `console.debug` line at most once per `WORD_INDEX_LOG_MS`, never a toast, and, while no
  index is held, builds one of the known list alone (`rebuildWordIndex()` over no entries) and
  draws it with `refreshWordMarks()`: the known words, the particles, the katakana words and the
  names need no deck. Its stamp and key stay unset, so the first deck answer with entries replaces
  it; an index already held (a deck read before Anki closed) stays as it was. `stale` answers
  count as fresh. After a successful mine
  `mineCue()` sets `wordIndexAskedAt` so the next ask goes out `WORD_INDEX_MINE_DELAY_MS` after
  it, once the background has expired its index.
- The storage listener: a change to any of `WORD_SETTINGS` runs `dropWordIndex()` (index and entries
  null, serial and generation on, stamp, key and `askedAt` 0), then `refreshWordMarks()` (plain text
  again at once, in place) and, with the colours on, `pollWordIndex()`. A change to `knownWords`
  alone runs `rebuildWordIndex()` (the index again from `wordEntries`, or from no entries while no
  deck answer is in hand, with the new `knownList()`, the serial on), a change to `katakanaKnown`,
  `particlesKnown` or `properNames` needs no new index (every look found under the old switch fails
  `sameSwitches()`); either is then drawn in place by `refreshWordMarks()`, with no drop and no
  ask, `refreshText()` replacing only the lines whose `drawKey()` moved. While `enabled` is off
  that draw waits: the lines keep their colours under the hidden root, and turning the add-on on
  runs the refresh, which compares against the index last drawn and the switches each look was
  found under, and so draws again just the lines the list or a switch changed meanwhile.
  `applySettings()` never rebuilds the transcript (only
  `state.transcriptRebuild`, set by `dropCues()` or a new panel, makes `renderTranscript()` build
  every line anew): a panel shown again renders what arrived while it was hidden
  (`renderTranscript()`) and then runs `refreshWordMarks()` and `highlightTranscript()`, so its
  lines catch up in place on the index and the active line; a style setting written (a slider
  dragged) leaves the lines and the line on screen as they are. `dropCues()` leaves the index alone
  (it belongs to the deck, not the video).
- Known words. `knownList(settings)` (pure): the lines of `knownWords`, trimmed, blank ones out,
  each once, in the order written. `markKnown()` answers the `mark-known` command (Alt+Shift+K in
  the manifest; like every command but the switch, not while `enabled` is off; the colours need
  not be on). `knownTarget()`: the selection when it lies inside the subtitle box or the
  transcript list (`selectedText()`, through `knownWordFrom()`; Yomitan selects the text it
  scanned while its popup is up), replaced by the index's own word like the rest; else the caret
  at `state.lastPointer` (`caretAt()`: Firefox's `caretPositionFromPoint`, Chrome's
  `caretRangeFromPoint`) inside what `renderText()` drew (`drawnAround()`: the line on screen or
  a transcript line's text), but only while the pointer is over the player
  (`state.pointerInPlayer`, set by `onPlayerMouseMove()` and cleared by `onPlayerMouseLeave()`:
  once it has left, the line under its last position has moved on or the transcript has
  scrolled) or the video is hover-paused (the line waits where it was for a pointer gone to a
  dictionary popup); otherwise there is no word under the pointer. The caret APIs answer the gap
  nearest the point, which over a character's right half is the gap after it, so
  `characterUnder()` takes the character whose box holds the point (`drawnCharacter()` and a
  one-character range's `getBoundingClientRect()`, the character before the gap first, then the
  one after, as Yomitan checks the box), else the gap as it came. There: a word span's text, and
  for an honorific prefix drawn as a span of its own the word span after it (`wordAfter()`:
  [お][風呂] marks 風呂); else the ICU segment (`segmentAt()`; an honorific prefix segment gives
  the segment after it, お|風呂; a single kanji takes the hiragana segments after it up to the
  first particle the matcher finds, `particleStarts()`, `markWords()` over an empty index with the
  particles counted as known, whose guard keeps the okurigana and a verb's inflection out of
  them: 食|べ|て is 食べて, 私|は|これ|が is 私, 本|を|読|ん|だ is 読んだ), through
  `knownWordFrom()` (one line, at most `MAX_WORD_LEN`, a letter or digit in it) and replaced by
  the index's own word when it knows the form (`entryWordFor()`: 食べる for 食べた, 勉強 for
  勉強している). A word `buildIndex()` drops, a particle (`buildIndex([], [word]).size === 0`), is
  refused with a warn toast (`X is a particle: the "Particles count as known" switch decides its
  colour`): on the list it would colour nothing; one on the list already (typed in the popup)
  still comes off it. The word comes off the list when it is on it, else goes on;
  `saveSettings({knownWords})` and a toast (`X marked as known`, `X is no longer marked as known`,
  `No word under the pointer` without one); every tab follows through the storage listener. A
  command comes from the browser, never from the page.
- Tests: `addon/tests/content.test.js` (`countingWords()` wraps `markWords` / `wordStarts`; `_loadContent.js` declares words.js's export with `var` for that).
  `_loadContent.js` also returns `onPlayerMouseLeave`, `knownList`, `rebuildWordIndex`,
  `segmentAt`, `entryWordFor`, `knownTarget` and `markKnown`, and its document has no selection,
  no caret API and no ranges unless a test puts them there.

### The popup (`addon/popup.html` / `popup.js`)

The "Word colours" section holds `#cardStatus` and its legend (the four states and a blue swatch,
`proper`, for names and Latin text "when switched on"), the `#cardStatusDeck` select (first option
value `""`), `#deck-hint`, and `#pitchAccent` with its legend (swatches in `popup.css`). What
colours without a card is folded into a drawer at the section's end, a `<details>` ("More word
colour options") built like the other drawers (`<summary>`, `<div class="drawer">`; closed, no
state kept; `section details` / `section summary` / `section .drawer` in `popup.css` let the
section's padding and line stand in for the drawer's own): `#particlesKnown`, `#katakanaKnown` and
`#properNames` ("Names and Latin text in blue"), all three unchecked by default, and the
`#knownWords` textarea (a typed field, saved at its change event; `readField()` stores it through
`knownWordsText()`, one word per line, each trimmed, blank lines out, as `knownList()` reads it)
with a hint naming Alt+Shift+K; none of these asks Anki. Alt+Shift+K writes `knownWords` from a YouTube tab while the
options page may hold the list focused for hours, so a focused text field keeps a change made
elsewhere out only while the viewer is typing in it (`typing()`: its value, as `readField()` reads
it, differs from `typedBaseline`, what `init()` or `onStorageChanged()` last put in or `flushSave()`
last sent); a list that skipped the marks on focus alone would save itself over them at its next
edit. `#ankiPitchField` sits in the "Anki, clips and server" drawer
after the word field. `renderDeckOptions(decks, seen, current)` keeps the automatic entry first
(`automaticDeckText(seen)`: `Automatic: <deck>` once Anki has been asked, `Automatic: no card mined
yet` for `seen` null, and the page's own `Automatic: the deck of the last mined card` for `seen`
undefined, before any ask or after a message that failed outright), then one option per name,
sorted, the stored value kept as an option even when unlisted (a select drops a value without an
option, and the setting would go with it at the next save); `init()` calls it before the `setField`
loop. `refreshDecks()` sends `{type: "ankiDecks"}` (answers numbered by `decksAsked`, an overtaken
one dropped; `decksOk` true for a listed set, false for a failure, null while an ask is out) and
paints `#deck-hint` only while a checkbox is on: the error (warn) when not ok, `No deck named <name>
in Anki` (error) for an unlisted manual deck, nothing for a listed one, `The last mined card's deck
<seen> is no longer in Anki; mine a card, or choose a deck` (warn), `Looking at <seen>`, or
`Automatic: no card mined yet — mine one, or choose a deck` (warn). Anki is asked from `init()` only
when `cardStatus || pitchAccent` is stored on (the first ask brings up AnkiConnect's dialog, which a
viewer who never uses the feature must not meet), from `onChange` after the debounced save when a
checkbox was turned on or `ankiUrl` edited (`decksCheckPending = "feature"`, only when a feature is
on in the form the save wrote) or the deck select changed (`"deck"`, always), and, while the newest
answer failed and a feature is on, again every `DECKS_RETRY_MS` (30 s) from a clock `init()` sets
beside the health refresh (`retryDecks()`; a good answer is never re-asked by the clock, and the
clock, like the health poll, ticks only while the page is visible, `document.hidden`), because the
options page (`options_ui.page` = popup.html) lives for hours. The save clears the hint when both
features are off. A deck, a checkbox or `ankiUrl` written by the other copy of the form (the options
page and the toolbar popup) lands through `onStorageChanged()`, which rebuilds the deck option from
Anki's last list (`renderDeckOptions(decksListed, decksSeen, value)`: a select shows nothing for a
value without an option) and asks Anki by the `onChange` rules less the save (`landed`: a deck, a
feature that landed checked, or the URL while a feature is on; a feature landing unchecked asks
nothing; both features off clears the hint). 
Tests: `addon/tests/popup.test.js`.
`addon/tests/settings.test.js` holds the defaults: the colours off, `particlesKnown` false,
`katakanaKnown` false, `properNames` false, `knownWords` empty.
