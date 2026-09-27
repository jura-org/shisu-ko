"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const words = require("../words");

const {
  STATUSES,
  PITCHES,
  plainText,
  plainWord,
  isKana,
  isHiragana,
  isKatakana,
  hasKanjiOrKatakana,
  moraCount,
  readingOf,
  parsePitch,
  pitchOf,
  usuallyKana,
  kanaReadingOf,
  statusOf,
  mergeStatus,
  buildIndex,
  wordStarts,
  markWords,
} = words;

// A plain-text pitch field may mark the drop with U+A71C.
const DROP = "ꜜ";

// Yomitan's {pitch-accents} field (26.9, `pitch-accent-list format='text'`), as its Anki
// template renderer writes it: createPronunciationText() plus applyClassStyles(), which inlines
// pronunciation-style.json and removes every class and data-* attribute. One inline-block <span>
// per mora with the kana and an empty line <span>; a high mora's line has a top border, the mora
// the pitch drops after a right border too. Checked against the real renderer's output.
function yomitanPitch(morae, n) {
  const high = (i) => (n === 0 ? i > 0 : n === 1 ? i < 1 : i > 0 && i < n);
  const LINE = "border-color:currentColor;";
  const HIGH = "display:block;user-select:none;pointer-events:none;position:absolute;top:0.1em;left:0;right:0;height:0;border-top-width:0.1em;border-top-style:solid;";
  const FALL = "right:-0.1em;height:0.4em;border-right-width:0.1em;border-right-style:solid;";
  let html = '<span style="display:inline;">';
  morae.forEach((mora, i) => {
    const drop = high(i) && !high(i + 1);
    html += `<span style="display:inline-block;position:relative;${drop ? "padding-right:0.1em;margin-right:0.1em;" : ""}">`;
    for (const ch of mora) html += `<span style="display:inline;">${ch}</span>`;
    html += `<span style="${LINE}${high(i) ? HIGH : ""}${drop ? FALL : ""}"></span></span>`;
  });
  return html + "</span>";
}

// The renderer's output for 橋 [2], verbatim.
const YOMITAN_HASHI_2 =
  '<span style="display:inline;"><span style="display:inline-block;position:relative;"><span style="display:inline;">は</span><span style="border-color:currentColor;"></span></span>' +
  '<span style="display:inline-block;position:relative;padding-right:0.1em;margin-right:0.1em;"><span style="display:inline;">し</span>' +
  '<span style="border-color:currentColor;display:block;user-select:none;pointer-events:none;position:absolute;top:0.1em;left:0;right:0;height:0;border-top-width:0.1em;border-top-style:solid;right:-0.1em;height:0.4em;border-right-width:0.1em;border-right-style:solid;"></span></span></span>';

// 鍵 [2] with a nasal second mora: the kana sits in a second inline-block <span> with the
// diacritic and the nasal indicator; the line <span> follows it.
const YOMITAN_KAGI_NASAL =
  '<span style="display:inline;"><span style="display:inline-block;position:relative;"><span style="display:inline;">か</span><span style="border-color:currentColor;"></span></span>' +
  '<span style="display:inline-block;position:relative;padding-right:0.1em;margin-right:0.1em;"><span style="display:inline-block;position:relative;"><span style="display:inline;">き</span>' +
  '<span style="position:absolute;width:0;height:0;opacity:0;">゚</span><span style="display:block;position:absolute;right:-0.125em;top:0.125em;width:0.375em;height:0.375em;border-radius:50%;box-sizing:border-box;z-index:1;border:1.5px solid #c83c28;"></span></span>' +
  '<span style="border-color:currentColor;display:block;user-select:none;pointer-events:none;position:absolute;top:0.1em;left:0;right:0;height:0;border-top-width:0.1em;border-top-style:solid;right:-0.1em;height:0.4em;border-right-width:0.1em;border-right-style:solid;"></span></span></span>';

// The runs of markWords as one line: a matched run carries (status,pitch) after its text.
function shape(runs) {
  return runs.map((run) => run.text + (run.status || run.pitch ? `(${run.status},${run.pitch})` : "")).join(" | ");
}

// The names are drawn blue here, as the tests of the matcher were written; "the name switch"
// below is where they are off.
const NAMES_ON = Object.freeze({ names: true });

function mark(text, entries, starts) {
  return shape(markWords(text, buildIndex(entries), starts, NAMES_ON));
}

// ------------------------------------------------------------------ constants

test("the object is frozen and lists the statuses, \"proper\" among them, and four patterns", () => {
  assert.ok(Object.isFrozen(words));
  assert.deepEqual(STATUSES, ["new", "learning", "learned", "suspended", "proper"]);
  assert.deepEqual(PITCHES, ["heiban", "atamadaka", "nakadaka", "odaka"]);
});

// ------------------------------------------------------------------ plainText / plainWord

test("plainText drops ruby readings with their text and every other tag", () => {
  assert.equal(plainText("<ruby>食<rt>た</rt></ruby>べる"), "食べる");
  assert.equal(plainText("<ruby>食<rp>(</rp><rt>た</rt><rp>)</rp></ruby>べる"), "食べる");
  assert.equal(plainText("<RUBY>食<RT>た</RT></RUBY>べる"), "食べる");
  assert.equal(plainText("これは<b>猫</b>です。"), "これは猫です。");
  assert.equal(plainText('<span class="x">猫</span>'), "猫");
});

// HTML lets </rt> and </rp> be left out before the next <rt>, <rp> or </ruby>. Yomitan writes
// them; a hand-written or third-party field may not, and match.js reads such a sentence, so the
// word must read the same way or the card never colours its word.
const RUBY_SHAPES = [
  ["<ruby>食<rt>た</rt></ruby>べる", "食べる", "たべる"],
  ["<ruby>食<rt>た</ruby>べる", "食べる", "たべる"],
  ["<ruby>食<rp>(</rp><rt>た</rt><rp>)</rp></ruby>べる", "食べる", "たべる"],
  ["<ruby>食<rp>(<rt>た<rp>)</ruby>べる", "食べる", "たべる"],
  ["<RUBY>食<RT>た</RUBY>べる", "食べる", "たべる"],
  ["<ruby>日本<rt>にほん</ruby><ruby>語<rt>ご</ruby>", "日本語", "にほんご"],
  ["<ruby>日本<rt>にほん</rt></ruby><ruby>語<rt>ご</rt></ruby>", "日本語", "にほんご"],
  ["<ruby><b>食</b><rt>た</ruby>べる", "食べる", "たべる"],
  [" 食[た]べる", "食べる", "たべる"],
  ["<b>食</b>べる", "食べる", ""],
];

test("plainText, plainWord and readingOf read a ruby whose end tags are left out", () => {
  for (const [html, word, reading] of RUBY_SHAPES) {
    assert.equal(plainWord(html), word, html);
    assert.equal(readingOf(html), reading, html);
    if (!html.includes("[")) assert.equal(plainText(html), word, html);
  }
});

test("plainWord agrees with SHISUKO_MATCH.normalize on every ruby shape", () => {
  // The background indexes a card's word with plainWord and matches its sentence with
  // match.js; the two must not drift apart on any shape of ruby again.
  const match = require("../match");
  for (const [html, word] of RUBY_SHAPES) {
    assert.equal(match.normalize(html), word, html);
    assert.equal(plainWord(html), match.normalize(html), html);
  }
});

test("plainText decodes entities, collapses whitespace and trims", () => {
  assert.equal(plainText("a&nbsp;b&amp;c"), "a b&c");
  assert.equal(plainText("&lt;tag&gt; &quot;q&quot; &#39;s&#39;"), "<tag> \"q\" 's'");
  assert.equal(plainText("  猫 \n\t 犬  "), "猫 犬");
  assert.equal(plainText(null), "");
  assert.equal(plainText(undefined), "");
  assert.equal(plainText(""), "");
});

test("plainWord drops bracket furigana and keeps the kanji", () => {
  assert.equal(plainWord(" 食[た]べる"), "食べる");
  assert.equal(plainWord("お 茶[ちゃ]"), "お茶");
  assert.equal(plainWord(" 日本語[にほんご]"), "日本語");
  assert.equal(plainWord("<ruby>食<rt>た</rt></ruby>べる"), "食べる");
  assert.equal(plainWord("<b>猫</b>"), "猫");
});

test("plainWord and readingOf stay quick on a passage in the field", () => {
  // The furigana regex scans from every character when no bracket follows: unbounded, a
  // 30,000-character line costs close to a second per note.
  const passage = "これは字幕の長い文です。".repeat(2500);
  let started = Date.now();
  const word = plainWord(passage);
  assert.ok(Date.now() - started < 50, "plainWord on a 30,000-character line");
  assert.ok(word.length > 40, "a passage is still not a word");
  started = Date.now();
  assert.equal(plainWord(" 食[た]べる<br>" + passage), "食べる");
  assert.equal(plainWord("\n \n" + passage.slice(0, 200)), passage.slice(0, 200));
  assert.ok(Date.now() - started < 50);
  // readingOf: unclosed <ruby> restart its lazy scan, and the furigana regex runs on the rest.
  const ruby = "<ruby>あ".repeat(2000) + "<rt>い</rt>" + "う".repeat(20000);
  started = Date.now();
  assert.equal(readingOf(ruby), "");
  assert.equal(readingOf("か".repeat(20000)), "");
  assert.ok(Date.now() - started < 50, "readingOf on a long field");
  // A reading of ordinary length is read as before.
  assert.equal(readingOf("<ruby>日本<rp>(</rp><rt>にほん</rt><rp>)</rp></ruby><ruby>語<rp>(</rp><rt>ご</rt><rp>)</rp></ruby>".repeat(8)), "にほんご".repeat(8));
});

test("plainWord and parsePitch stay quick on a field of tags never closed", () => {
  // A "<" without its ">" once made every tag pattern scan to the end of the field from every
  // "<" after it: a 100,000-character run cost seconds per note, on the background's one thread.
  const fieldOf = (list) => {
    const out = {};
    list.forEach(([name, value], order) => {
      out[name] = { value, order };
    });
    return out;
  };
  const started = Date.now();
  assert.ok(plainWord("<".repeat(100000) + "猫").length > 40, "the run of < is text, and no word");
  assert.equal(plainWord("x<".repeat(15000)).length, 320);
  assert.ok(plainWord("<rt ".repeat(10000) + "あ".repeat(100000)).length > 40);
  // Unclosed <rt> still restart the scan for </rt>, so the raw value is cut before any pattern
  // runs; what is left of this one is tags only.
  assert.equal(plainWord("<rt>".repeat(10000) + "あ".repeat(100000)), "");
  assert.equal(plainText("<".repeat(100000)).length, 16000);
  assert.equal(parsePitch("<".repeat(100000)), null);
  assert.equal(parsePitch("<rt>".repeat(10000) + "あ".repeat(100000)), null);
  assert.equal(parsePitch('<span style="display:inline-block;position:relative;"'.repeat(3000)), null);
  assert.equal(parsePitch('<span style="'.repeat(10000)), null);
  assert.equal(pitchOf(fieldOf([["Word", "猫"], ["Pitch", "<".repeat(100000)]]), {}), null);
  assert.equal(pitchOf(fieldOf([["Word", "<".repeat(100000)], ["Pitch", "[2]"]]), {}), "nakadaka");
  assert.ok(Date.now() - started < 200, "a field of tags never closed must cost milliseconds");
  // Markup of ordinary length reads as before, a word after a long passage included.
  assert.equal(plainWord(" 食[た]べる<br>" + "これは字幕の長い文です。".repeat(2500)), "食べる");
  assert.equal(plainText('<span class="x" data-y="1">猫</span><br>犬'), "猫 犬");
  assert.equal(parsePitch(YOMITAN_HASHI_2, "", "橋"), "odaka");
  assert.equal(parsePitch("<ol><li>" + yomitanPitch(["に", "ほ", "ん", "ご"], 0) + "</li></ol>"), "heiban");
});

test("plainWord takes the first line only and is empty when nothing is left", () => {
  assert.equal(plainWord("猫<br>ねこ"), "猫");
  assert.equal(plainWord("<div>猫</div><div>ねこ</div>"), "猫");
  assert.equal(plainWord("<br>猫"), "猫");
  assert.equal(plainWord(""), "");
  assert.equal(plainWord("<b></b>"), "");
  assert.equal(plainWord(null), "");
});

// ------------------------------------------------------------------ characters

test("the character classes tell hiragana, katakana and kanji apart", () => {
  assert.ok(isHiragana("あ"));
  assert.ok(!isHiragana("ア"));
  assert.ok(isKatakana("ア"));
  assert.ok(isKatakana("ｶ"));
  assert.ok(!isKatakana("あ"));
  assert.ok(isKana("ん") && isKana("ッ"));
  assert.ok(!isKana("漢") && !isKana("a") && !isKana("ー"));
  assert.ok(hasKanjiOrKatakana("食べる"));
  assert.ok(hasKanjiOrKatakana("コーヒー"));
  assert.ok(hasKanjiOrKatakana("人々"));
  assert.ok(!hasKanjiOrKatakana("たべる"));
  assert.ok(!hasKanjiOrKatakana("らーめん"));
  assert.ok(!hasKanjiOrKatakana(""));
  assert.ok(!hasKanjiOrKatakana(null));
});

test("moraCount counts kana, folds the small ones and gives ー a mora", () => {
  assert.equal(moraCount("きょうと"), 3);
  assert.equal(moraCount("トーキョー"), 4);
  assert.equal(moraCount("はし"), 2);
  assert.equal(moraCount("がっこう"), 4);
  assert.equal(moraCount("ふぁ"), 1);
  assert.equal(moraCount("abc"), 0);
  assert.equal(moraCount("漢字"), 0);
  assert.equal(moraCount(""), 0);
  assert.equal(moraCount(null), 0);
});

// ------------------------------------------------------------------ readingOf

test("readingOf takes the ruby text, the bracket furigana or the kana field", () => {
  assert.equal(readingOf("<ruby>食<rt>た</rt></ruby>べる"), "たべる");
  assert.equal(readingOf("<ruby>食<rp>(</rp><rt>た</rt><rp>)</rp></ruby>べる"), "たべる");
  assert.equal(readingOf("<ruby>日本<rt>にほん</rt></ruby><ruby>語<rt>ご</rt></ruby>"), "にほんご");
  assert.equal(readingOf(" 食[た]べる"), "たべる");
  assert.equal(readingOf("お 茶[ちゃ]"), "おちゃ");
  assert.equal(readingOf("たべる"), "たべる");
  assert.equal(readingOf("<b>コーヒー</b>"), "コーヒー");
});

test("readingOf is empty unless what is left is kana", () => {
  assert.equal(readingOf("食べる"), "");
  assert.equal(readingOf("taberu"), "");
  assert.equal(readingOf(""), "");
  assert.equal(readingOf(null), "");
});

// ------------------------------------------------------------------ parsePitch

test("parsePitch reads a category name, the first one by position", () => {
  assert.equal(parsePitch("heiban"), "heiban");
  assert.equal(parsePitch("heiban, odaka"), "heiban");
  assert.equal(parsePitch("odaka, heiban"), "odaka");
  assert.equal(parsePitch("Atamadaka"), "atamadaka");
  assert.equal(parsePitch("<b>Nakadaka</b>"), "nakadaka");
  assert.equal(parsePitch("頭高"), "atamadaka");
  assert.equal(parsePitch("平板"), "heiban");
  assert.equal(parsePitch("中高"), "nakadaka");
  assert.equal(parsePitch("尾高 (heiban)"), "odaka");
});

test("parsePitch reads a position in brackets, Yomitan's list included", () => {
  assert.equal(parsePitch("[0]"), "heiban");
  assert.equal(parsePitch("<ol><li>[0]</li><li>[3]</li></ol>"), "heiban");
  assert.equal(parsePitch("<ol><li>[1]</li><li>[0]</li></ol>"), "atamadaka");
  assert.equal(parsePitch("[2]", "はし", ""), "odaka");
  assert.equal(parsePitch("[2]", "さかな", ""), "nakadaka");
  assert.equal(parsePitch("[3]", "こころ", ""), "odaka");
});

test("parsePitch reads a bare number", () => {
  assert.equal(parsePitch("0"), "heiban");
  assert.equal(parsePitch(" 1 "), "atamadaka");
  assert.equal(parsePitch("2", "はし", ""), "odaka");
  assert.equal(parsePitch("2", "", "さかな"), "nakadaka");
});

test("parsePitch reads the moras Yomitan's {pitch-accents} draws", () => {
  assert.equal(parsePitch(YOMITAN_HASHI_2, "はし", "橋"), "odaka");
  assert.equal(yomitanPitch(["は", "し"], 2), YOMITAN_HASHI_2);
  assert.equal(parsePitch(yomitanPitch(["は", "し"], 0), "はし", "端"), "heiban");
  assert.equal(parsePitch(yomitanPitch(["は", "し"], 1), "はし", "箸"), "atamadaka");
  assert.equal(parsePitch(yomitanPitch(["さ", "か", "な"], 2), "さかな", "魚"), "nakadaka");
  assert.equal(parsePitch(yomitanPitch(["こ", "こ", "ろ"], 3), "こころ", "心"), "odaka");
  // The moras are counted as drawn: きょ is one, and the reading or word is not consulted.
  assert.equal(parsePitch(yomitanPitch(["と", "う", "きょ", "う"], 0)), "heiban");
  assert.equal(parsePitch(yomitanPitch(["きょ", "う", "だ", "い"], 1)), "atamadaka");
  assert.equal(parsePitch(yomitanPitch(["に", "ほ", "ん"], 2), "", ""), "nakadaka");
  assert.equal(parsePitch(yomitanPitch(["に", "ほ", "ん"], 3), "", ""), "odaka");
  // A nasal mora's extra inline-block span is not a mora.
  assert.equal(parsePitch(YOMITAN_KAGI_NASAL, "かぎ", "鍵"), "odaka");
  // Several patterns: <ol><li>…</li></ol>, and the first counts, as with the positions.
  const list = (ns, morae) => `<ol>${ns.map((n) => `<li>${yomitanPitch(morae, n)}</li>`).join("")}</ol>`;
  assert.equal(parsePitch(list([0, 3], ["こ", "こ", "ろ"])), "heiban");
  assert.equal(parsePitch(list([3, 0], ["こ", "こ", "ろ"])), "odaka");
  assert.equal(parsePitch(list([2, 0], ["は", "し"])), "odaka");
  // The disambiguation the template may put before it changes nothing.
  assert.equal(parsePitch(`<em>(はし only) </em>${YOMITAN_HASHI_2}`, "はし", "橋"), "odaka");
});

test("parsePitch reads the drop mark of a plain-text field", () => {
  assert.equal(parsePitch(`はし${DROP}`), "odaka");
  assert.equal(parsePitch(`は${DROP}し`), "atamadaka");
  assert.equal(parsePitch(`たま${DROP}ご`), "nakadaka");
  assert.equal(parsePitch(`きょ${DROP}うと`), "atamadaka");
  // Kana alone, with no mark, says nothing: the viewer's oldest cards hold the bare reading in the
  // pitch field whatever the pattern (302 of them), and heiban was wrong for most.
  assert.equal(parsePitch("はし"), null);
  assert.equal(parsePitch("こころ"), null);
  // Kana inside markup this does not read is not: Yomitan's own drawing never has ꜜ, so a
  // kana-only text can be any pattern behind unknown markup.
  assert.equal(parsePitch("<span>コーヒー</span>"), null);
  assert.equal(parsePitch("<b>はし</b>"), null);
});

test("parsePitch counts the moras from the pitch text, else the reading, else the word", () => {
  // The pitch text is kana: its own count decides, whatever the reading says.
  assert.equal(parsePitch(`はし${DROP}`, "さかな", "魚"), "odaka");
  // A position alone: the reading, then the word.
  assert.equal(parsePitch("[3]", "さかな", ""), "odaka");
  assert.equal(parsePitch("[3]", "", "さかな"), "odaka");
  assert.equal(parsePitch("[3]", "魚", "魚"), "nakadaka");
  // Nothing to count: nakadaka, the more common of the two.
  assert.equal(parsePitch("[2]"), "nakadaka");
  assert.equal(parsePitch("[2]", null, undefined), "nakadaka");
  // 0 and 1 need no count.
  assert.equal(parsePitch("[0]", "", ""), "heiban");
  assert.equal(parsePitch("[1]", "", ""), "atamadaka");
});

test("parsePitch is null when the field says nothing it understands", () => {
  assert.equal(parsePitch(""), null);
  assert.equal(parsePitch(null), null);
  assert.equal(parsePitch(undefined), null);
  assert.equal(parsePitch("???"), null);
  assert.equal(parsePitch("<b></b>"), null);
  assert.equal(parsePitch("食べる"), null);
  assert.equal(parsePitch("[a]"), null);
});

// ------------------------------------------------------------------ pitchOf

function fields(list) {
  const out = {};
  list.forEach(([name, value], order) => {
    out[name] = { value, order };
  });
  return out;
}

test("pitchOf finds the pitch field by name and the reading beside it", () => {
  const note = fields([
    ["Word", "橋"],
    ["Reading", "はし"],
    ["PitchAccent", "[2]"],
  ]);
  assert.equal(pitchOf(note, {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "魚"], ["Reading", "さかな"], ["Pitch Accent", "[2]"]]), {}), "nakadaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["Furigana", " 橋[はし]"], ["アクセント", "2"]]), {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", "はし"], ["Pitch", "heiban"]]), {}), "heiban");
});

test("pitchOf never takes a sentence's reading for the word's", () => {
  // Yomitan's {sentence-furigana} in a field named SentenceFurigana: the word's own reading
  // is elsewhere or nowhere, and the sentence's mora count must not make 橋 [2] nakadaka.
  const sentence = " 橋[はし]を 渡[わた]る";
  assert.equal(pitchOf(fields([["Word", "はし"], ["SentenceFurigana", sentence], ["PitchAccent", "[2]"]]), {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["SentenceFurigana", sentence], ["Reading", "はし"], ["PitchAccent", "[2]"]]), {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["SentenceReading", "はしをわたる"], ["PitchAccent", "[2]"]]), {}), "nakadaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["例文読み", "はしをわたる"], ["PitchAccent", "[2]"]]), {}), "nakadaka");
  // A word field the sentence's reading would have hidden.
  assert.equal(pitchOf(fields([["Expression", "さかな"], ["Sentence", "魚を食べる"], ["SentenceFurigana", " 魚[さかな]を 食[た]べる"], ["Pitch", "[3]"]]), {}), "odaka");
});

test("pitchOf prefers the field the settings name and falls back when it is missing", () => {
  const note = fields([
    ["Word", "橋"],
    ["Reading", "はし"],
    ["PitchAccent", "[0]"],
    ["Notes", "[2]"],
  ]);
  assert.equal(pitchOf(note, { ankiPitchField: "Notes" }), "odaka");
  assert.equal(pitchOf(note, { ankiPitchField: " Notes " }), "odaka");
  assert.equal(pitchOf(note, { ankiPitchField: "Missing" }), "heiban");
  assert.equal(pitchOf(note, { ankiPitchField: "" }), "heiban");
  // A name in another case than the note type's is the same field.
  assert.equal(pitchOf(note, { ankiPitchField: "notes" }), "odaka");
  assert.equal(pitchOf(note, { ankiPitchField: "NOTES" }), "odaka");
});

// Note types spell one field Picture or picture (Lapis capitalises, Eminent does not), and a
// setting in the other case used to find nothing: every mine into an Eminent card failed.
test("fieldKey names a note's field in whatever case the note type spells it", () => {
  const { fieldKey } = words;
  const eminent = fields([["wordDictionaryForm", "切り"], ["sentence", "切り。"], ["sentenceAudio", ""], ["picture", ""]]);
  assert.equal(fieldKey(eminent, "Picture"), "picture");
  assert.equal(fieldKey(eminent, "SentenceAudio"), "sentenceAudio");
  assert.equal(fieldKey(eminent, "Sentence"), "sentence");
  assert.equal(fieldKey(eminent, "picture"), "picture");
  assert.equal(fieldKey(eminent, " picture "), "picture", "the setting is trimmed");
  assert.equal(fieldKey(eminent, "Image"), null);
  assert.equal(fieldKey(eminent, ""), null);
  assert.equal(fieldKey(eminent, "   "), null);
  assert.equal(fieldKey(eminent, undefined), null);
  assert.equal(fieldKey(null, "Picture"), null);
  // An object's own names only: nothing inherited is a field.
  assert.equal(fieldKey(eminent, "constructor"), null);
  assert.equal(fieldKey(eminent, "toString"), null);
  // Two fields differing in case alone: the exact name is taken, an inexact one is not guessed.
  const both = fields([["Picture", ""], ["picture", ""]]);
  assert.equal(fieldKey(both, "picture"), "picture");
  assert.equal(fieldKey(both, "Picture"), "Picture");
  assert.equal(fieldKey(both, "PICTURE"), null);
});

test("the word field the settings name is found in any case too", () => {
  const note = fields([["Expression", "更に"], ["Word", "さらに"], ["Reading", "さらに"], ["Glossary", "(adv, uk)"]]);
  assert.equal(kanaReadingOf(note, { ankiWordField: "Expression" }), "さらに");
  assert.equal(kanaReadingOf(note, { ankiWordField: "expression" }), "さらに");
  // The word field is skipped as a reading source whatever case names it: here it is named like a
  // reading field and comes first, so without the skip its kanji would be taken for the reading.
  assert.equal(kanaReadingOf(fields([["reading", "更に"], ["KanaReading", "さらに"], ["Notes", "(uk)"]]), { ankiWordField: "Reading" }), "さらに");
});

test("pitchOf takes the lowest-order matching field and never reads the pitch field as a reading", () => {
  const note = fields([
    ["Word", "橋"],
    ["PitchAccentReading", "[2]"],
    ["Accent", "[1]"],
    ["Reading", "はし"],
  ]);
  // Both match the pitch rule; the first by order is the pitch field, and the reading comes
  // from the field after it, not from the pitch field's own name.
  assert.equal(pitchOf(note, {}), "odaka");
  // The pitch field in first place is the note's word by the order-0 rule; "[2]" is no kana, so
  // the count is unknown.
  const pitchFirst = fields([["Pitch", "[2]"], ["Word", "橋"]]);
  assert.equal(pitchOf(pitchFirst, {}), "nakadaka");
});

test("pitchOf counts the word's moras when there is no reading", () => {
  assert.equal(pitchOf(fields([["Word", "さかな"], ["Pitch", "[3]"]]), {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "魚"], ["Pitch", "[3]"]]), {}), "nakadaka");
  assert.equal(pitchOf(fields([["Word", "魚"], ["Kana", "さかな"], ["Pitch", "[3]"]]), { ankiWordField: "Kana" }), "odaka");
  assert.equal(pitchOf(fields([["Word", " 魚[さかな]"], ["Pitch", "[3]"]]), {}), "nakadaka");
});

test("pitchOf moves on to the next pitch field when the first one has no readable value", () => {
  // Jidoujisho's graph (Yomitan's createPronunciationGraphJJ()) draws its moras as dots of radius
  // 5, which this does not read; the position sits in the field after it.
  const graph =
    '<svg xmlns="http://www.w3.org/2000/svg" width="42px" height="45px" viewBox="0 0 70 75"><path d="m 16,30 35,-25" style="fill:none;stroke:currentColor;stroke-width:1.5;"></path>' +
    '<circle r="5" cx="16" cy="30" style="opacity:1;fill:currentColor;"></circle><circle r="5" cx="51" cy="5" style="opacity:1;fill:currentColor;"></circle></svg>';
  const note = fields([
    ["Word", "橋"],
    ["Reading", "はし"],
    ["PitchAccent", graph],
    ["PitchPosition", "[2]"],
  ]);
  assert.equal(pitchOf(note, {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", "はし"], ["PitchAccent", ""], ["Accent", "[1]"]]), {}), "atamadaka");
  // The named field is taken as it is, readable or not.
  assert.equal(pitchOf(note, { ankiPitchField: "PitchAccent" }), null);
  assert.equal(pitchOf(note, { ankiPitchField: "PitchPosition" }), "odaka");
});

test("pitchOf is null without a pitch field or a readable value", () => {
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", "はし"]]), {}), null);
  assert.equal(pitchOf(fields([["Word", "橋"], ["Pitch", ""]]), {}), null);
  assert.equal(pitchOf(fields([["Word", "橋"], ["Pitch", "?"]]), {}), null);
  assert.equal(pitchOf({}, {}), null);
  assert.equal(pitchOf(null, null), null);
  assert.equal(pitchOf({ Pitch: "[0]" }, {}), null);
});

// Yomitan's {pitch-accent-graphs} as its Anki template renderer writes it:
// createPronunciationGraph() plus applyClassStyles(), which inlines pronunciation-style.json and
// removes every class. Two <path>s for the line and its dashed tail, then one <circle> of radius 15
// per mora (cy 25 high, 75 low), the mora the pitch drops after hollow with a radius-5 dot inside
// it, and the triangle <path> of the particle after the word, translated to its place.
function yomitanGraph(moras, n) {
  const high = (i) => (n === 0 ? i > 0 : n === 1 ? i < 1 : i > 0 && i < n);
  const LINE = "fill:none;stroke-width:5;stroke:currentColor;";
  const DOT = "stroke-width:5;fill:currentColor;stroke:currentColor;";
  const points = [];
  let dots = "";
  for (let i = 0; i < moras; i++) {
    const [x, y] = [i * 50 + 25, high(i) ? 25 : 75];
    if (high(i) && !high(i + 1)) dots += `<circle cx="${x}" cy="${y}" r="15" style="${LINE}"></circle><circle cx="${x}" cy="${y}" r="5" style="fill:currentColor;"></circle>`;
    else dots += `<circle cx="${x}" cy="${y}" r="15" style="${DOT}"></circle>`;
    points.push(`${x} ${y}`);
  }
  const [tx, ty] = [moras * 50 + 25, high(moras) ? 25 : 75];
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" focusable="false" viewBox="0 0 ${50 * (moras + 1)} 100" style="display:inline-block;vertical-align:middle;height:1.5em;">` +
    `<path d="M${points.join(" L")}" style="${LINE}"></path><path d="M${points[moras - 1]} L${tx} ${ty}" style="${LINE}stroke-dasharray:5 5;"></path>` +
    `${dots}<path d="M0 13 L15 -13 L-15 -13 Z" transform="translate(${tx},${ty})" style="${LINE}"></path></svg>`
  );
}

test("parsePitch reads the SVG graph Yomitan's {pitch-accent-graphs} draws", () => {
  // The five shapes of the viewer's deck: 家 (いえ) low, high hollow; 動画 (どうが) low, high,
  // high; 観 (かん) high hollow, low; お母さん low, high hollow, low, low, low; 一週間 low, high,
  // high hollow, low, low, low.
  const ie = yomitanGraph(2, 2);
  assert.ok(ie.includes('<circle cx="25" cy="75" r="15" style="stroke-width:5;fill:currentColor;stroke:currentColor;"></circle>'));
  assert.ok(ie.includes('<circle cx="75" cy="25" r="15" style="fill:none;stroke-width:5;stroke:currentColor;"></circle><circle cx="75" cy="25" r="5"'));
  assert.ok(ie.includes('transform="translate(125,75)"'));
  assert.equal(parsePitch(ie), "odaka");
  assert.equal(parsePitch(yomitanGraph(3, 0)), "heiban");
  assert.equal(parsePitch(yomitanGraph(2, 1)), "atamadaka");
  assert.equal(parsePitch(yomitanGraph(5, 2)), "nakadaka");
  assert.equal(parsePitch(yomitanGraph(6, 3)), "nakadaka");
  // The circles are the moras: the dot inside the hollow one is not, and no reading or word is
  // asked to tell odaka from nakadaka.
  assert.equal(parsePitch(yomitanGraph(3, 3), "はし", "橋"), "odaka");
  assert.equal(parsePitch(yomitanGraph(3, 2), "はし", "橋"), "nakadaka");
  // Several patterns: the first counts, as with the positions.
  assert.equal(parsePitch(`<ol><li>${yomitanGraph(2, 2)}</li><li>${yomitanGraph(2, 0)}</li></ol>`), "odaka");
  assert.equal(parsePitch(`<ol><li>${yomitanGraph(2, 0)}</li><li>${yomitanGraph(2, 2)}</li></ol>`), "heiban");
  // Dots too small to be moras (Jidoujisho's graph) or none at all draw nothing this reads.
  assert.equal(parsePitch('<svg viewBox="0 0 50 100"><path d="M25 75"></path></svg>'), null);
  assert.equal(parsePitch('<svg><circle r="5" cx="16" cy="30" style="opacity:1;fill:currentColor;"></circle></svg>'), null);
});

test("pitchOf reads a graph field before the position field after it", () => {
  const note = fields([["Word", "家"], ["Reading", "いえ"], ["PitchAccent", yomitanGraph(2, 2)], ["PitchPosition", "[0]"]]);
  assert.equal(pitchOf(note, {}), "odaka");
  assert.equal(pitchOf(note, { ankiPitchField: "PitchPosition" }), "heiban");
});

test("pitchOf falls back to a reading field that draws the pitch, never to a plain reading", () => {
  // Yomitan's {pitch-accents} is often the reading field itself.
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", YOMITAN_HASHI_2]]), {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "家"], ["Reading", yomitanGraph(2, 2)]]), {}), "odaka");
  // A pitch field that reads nothing (the bare reading, heiban no more) gives way to it; one that
  // reads wins.
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", YOMITAN_HASHI_2], ["Pitch", "はし"]]), {}), "odaka");
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", YOMITAN_HASHI_2], ["Pitch", "[0]"]]), {}), "heiban");
  // A plain reading draws nothing, and a sentence's reading is never asked.
  assert.equal(pitchOf(fields([["Word", "橋"], ["Reading", "はし"]]), {}), null);
  assert.equal(pitchOf(fields([["Word", "橋"], ["Furigana", " 橋[はし]"]]), {}), null);
  assert.equal(pitchOf(fields([["Word", "橋"], ["SentenceReading", YOMITAN_HASHI_2]]), {}), null);
});

// ------------------------------------------------------------------ usuallyKana / kanaReadingOf

// Jitendex's glossary for 更に, cut down: the tag is a span whose title says it.
const JITENDEX_SARANI =
  '<div><ol><li><span title="adverb (fukushi)">adverb</span><span title="word usually written using kana alone">kana</span>' +
  "<ul><li>furthermore; again; after all; more and more</li></ul></li></ol></div>";

test("usuallyKana reads Jitendex's title and JMdict's note, in any case", () => {
  assert.equal(usuallyKana(fields([["Word", "更に"], ["SecondaryDef", JITENDEX_SARANI]])), true);
  assert.equal(usuallyKana(fields([["Word", "更に"], ["Glossary", "Word Usually Written Using Kana Alone"]])), true);
  // The tag deep in a glossary longer than any field limit elsewhere still counts.
  assert.equal(usuallyKana(fields([["Word", "更に"], ["Glossary", "x".repeat(50000) + JITENDEX_SARANI]])), true);
  assert.equal(usuallyKana(fields([["Word", "勝手"], ["Glossary", "<i>(adj-na, n)</i> selfishness"]])), false);
  assert.equal(usuallyKana({}), false);
  assert.equal(usuallyKana(null), false);
});

test("usuallyKana reads a uk item of Yomitan's plain tag list, and nothing else in parentheses", () => {
  assert.equal(usuallyKana(fields([["Glossary", "(adv, uk, JMdict (English)) furthermore"]])), true);
  assert.equal(usuallyKana(fields([["Glossary", "<i>(uk, JMdict)</i> furthermore"]])), true);
  assert.equal(usuallyKana(fields([["Glossary", "(adv,uk)"]])), true);
  assert.equal(usuallyKana(fields([["Glossary", "(<span>uk</span>, adv)"]])), true);
  // Prose: the United Kingdom is not the tag, in any case or spelling.
  assert.equal(usuallyKana(fields([["Glossary", "(UK) lorry"]])), false);
  assert.equal(usuallyKana(fields([["Glossary", "(Uk, adv)"]])), false);
  assert.equal(usuallyKana(fields([["Glossary", "United Kingdom (uk-based)"]])), false);
  assert.equal(usuallyKana(fields([["Glossary", "a firm (in the uk) of note"]])), false);
  assert.equal(usuallyKana(fields([["Glossary", "uk, adv"]])), false);
});

test("usuallyKana stays quick on a field of nothing but brackets", () => {
  const started = Date.now();
  for (const ch of ["(", "<", "(<", "(a,"]) {
    assert.equal(usuallyKana(fields([["Word", "更に"], ["Glossary", ch.repeat(Math.ceil(100000 / ch.length))]])), false);
  }
  assert.ok(Date.now() - started < 1000, `took ${Date.now() - started} ms`);
});

test("kanaReadingOf gives the reading of a word usually written in kana", () => {
  const sarani = [["Word", "更に"], ["Reading", "さらに"], ["Furigana", "<ruby>更<rt>さら</rt></ruby>に"], ["SecondaryDef", JITENDEX_SARANI]];
  assert.equal(kanaReadingOf(fields(sarani), {}), "さらに");
  // Without the tag, a reading indexed would be found inside other words (勝手 in 向かって).
  assert.equal(kanaReadingOf(fields(sarani.slice(0, 3)), {}), "");
  // A word without kanji is indexed as it is.
  assert.equal(kanaReadingOf(fields([["Word", "さらに"], ["Reading", "さらに"], ["SecondaryDef", JITENDEX_SARANI]]), {}), "");
  assert.equal(kanaReadingOf(fields([["Word", "カメラ"], ["Reading", "かめら"], ["SecondaryDef", JITENDEX_SARANI]]), {}), "");
  // No reading field at all.
  assert.equal(kanaReadingOf(fields([["Word", "更に"], ["SecondaryDef", JITENDEX_SARANI]]), {}), "");
});

test("kanaReadingOf reads a ruby field when there is no reading field, and skips the sentence's", () => {
  const note = fields([
    ["Word", "更に"],
    ["Sentence", "さらに言うと"],
    ["SentenceFurigana", "さらに 言[い]うと"],
    ["Furigana", "<ruby>更<rt>さら</rt></ruby>に"],
    ["Glossary", "(adv, uk, JMdict (English)) furthermore"],
  ]);
  assert.equal(kanaReadingOf(note, {}), "さらに");
});

test("kanaReadingOf never reads the word field or a pitch field as the reading", () => {
  // The word field the settings name is itself called Reading, and a pitch field's name says
  // reading too; both come before the real reading.
  const note = fields([
    ["Front", "x"],
    ["Reading", "更に"],
    ["PitchReading", "かって"],
    ["WordFurigana", " 更[さら]に"],
    ["Glossary", JITENDEX_SARANI],
  ]);
  assert.equal(kanaReadingOf(note, { ankiWordField: "Reading" }), "さらに");
});

test("kanaReadingOf drops a reading longer than a word", () => {
  const long = "あ".repeat(words.MAX_WORD_LEN + 1);
  assert.equal(kanaReadingOf(fields([["Word", "更に"], ["Reading", long], ["SecondaryDef", JITENDEX_SARANI]]), {}), "");
});

test("a reading entry colours the word as the subtitle writes it", () => {
  const deck = [["更に", "learned", null], ["さらに", "learned", null]];
  assert.equal(mark("けど、僕の学校、さらに言うと、", deck), "けど、僕の学校、 | さらに(learned,null) | 言うと、");
});

// ------------------------------------------------------------------ statusOf / mergeStatus

test("statusOf reads the five query sets in order of precedence", () => {
  const sets = {
    suspended: new Set([1]),
    unsuspended: new Set([2, 3, 4, 5]),
    new: new Set([2]),
    learning: new Set([3]),
    review: new Set([4]),
  };
  assert.equal(statusOf(sets, 1), "suspended");
  assert.equal(statusOf(sets, 2), "new");
  assert.equal(statusOf(sets, 3), "learning");
  assert.equal(statusOf(sets, 4), "learned");
  // Unsuspended but in no queue: buried in the learning queue.
  assert.equal(statusOf(sets, 5), "learning");
  // Not in the deck at all.
  assert.equal(statusOf(sets, 6), null);
  assert.equal(statusOf({}, 1), null);
  assert.equal(statusOf(null, 1), null);
});

test("statusOf lets a card that is both new and suspended count as suspended", () => {
  const sets = { suspended: new Set([1]), unsuspended: new Set(), new: new Set([1]), learning: new Set(), review: new Set() };
  assert.equal(statusOf(sets, 1), "suspended");
});

test("mergeStatus takes the least progress and keeps suspended only when both are", () => {
  assert.equal(mergeStatus(null, "learned"), "learned");
  assert.equal(mergeStatus("new", null), "new");
  assert.equal(mergeStatus(null, null), null);
  assert.equal(mergeStatus(undefined, "learning"), "learning");
  assert.equal(mergeStatus("learned", "new"), "new");
  assert.equal(mergeStatus("learning", "learned"), "learning");
  assert.equal(mergeStatus("learned", "learned"), "learned");
  assert.equal(mergeStatus("new", "learning"), "new");
  assert.equal(mergeStatus("suspended", "suspended"), "suspended");
  assert.equal(mergeStatus("suspended", "learned"), "learned");
  assert.equal(mergeStatus("new", "suspended"), "new");
  assert.equal(mergeStatus("bogus", "learned"), "learned");
});

test("mergeStatus lets any card beat \"proper\", which no card has", () => {
  assert.equal(mergeStatus("proper", "new"), "new");
  assert.equal(mergeStatus("learned", "proper"), "learned");
  assert.equal(mergeStatus("proper", "suspended"), "suspended");
  assert.equal(mergeStatus("proper", null), "proper");
  assert.equal(mergeStatus(null, "proper"), "proper");
  assert.equal(mergeStatus("proper", "proper"), "proper");
});

// ------------------------------------------------------------------ buildIndex

test("buildIndex trims words, drops empty and overlong ones and merges duplicates", () => {
  const index = buildIndex([
    [" 猫 ", "learned", null],
    ["猫", "new", "heiban"],
    ["猫", "suspended", "odaka"],
    ["", "new", null],
    ["   ", "new", null],
    ["a".repeat(41), "new", null],
    ["a".repeat(40), "new", null],
    null,
    "犬",
    ["犬"],
  ]);
  assert.ok(Object.isFrozen(index));
  assert.equal(index.size, 3);
  assert.deepEqual(index.exact.get("猫"), { word: "猫", status: "new", pitch: "heiban", bounded: false });
  assert.deepEqual(index.exact.get("犬"), { word: "犬", status: null, pitch: null, bounded: false });
  assert.ok(index.exact.has("a".repeat(40)));
  assert.equal(index.maxLen, 40);
  assert.equal(buildIndex([]).size, 0);
  assert.equal(buildIndex(null).size, 0);
});

test("buildIndex ignores a status or pitch it does not know", () => {
  const index = buildIndex([["猫", "bogus", "flat"]]);
  assert.deepEqual(index.exact.get("猫"), { word: "猫", status: null, pitch: null, bounded: false });
});

test("buildIndex cuts a stem from the words with kanji or katakana, and from a kana word with two kana to spare", () => {
  const index = buildIndex([
    ["勉強する", "new", null],
    ["美しい", "new", null],
    ["食べる", "new", null],
    ["書く", "new", null],
    ["サボる", "new", null],
    ["日本語", "new", null],
    ["コーヒー", "new", null],
    ["たべる", "new", null],
    ["する", "new", null],
    ["見", "new", null],
  ]);
  const kinds = (stem) => (index.stems.get(stem) || []).map((item) => `${item.entry.word}:${item.kind}`);
  assert.deepEqual(kinds("勉強"), ["勉強する:suru"]);
  assert.deepEqual(kinds("美し"), ["美しい:i-adj"]);
  assert.deepEqual(kinds("食べ"), ["食べる:ru"]);
  assert.deepEqual(kinds("書"), ["書く:く"]);
  assert.deepEqual(kinds("サボ"), ["サボる:ru"]);
  assert.deepEqual(kinds("たべ"), ["たべる:ru"]);
  assert.equal(index.stems.size, 6);
  assert.equal(index.maxStemLen, 2);
  // Kana-only words need a boundary after them, in every form.
  assert.equal(index.exact.get("たべる").bounded, true);
  assert.equal(index.exact.get("する").bounded, true);
  assert.equal(index.exact.get("食べる").bounded, false);
});

test("buildIndex gives a kana-only word a stem of two kana at least", () => {
  const index = buildIndex([
    ["かける", "new", null],
    ["つくる", "new", null],
    ["しまう", "new", null],
    ["もらう", "new", null],
    ["おいしい", "new", null],
    ["すごい", "new", null],
    ["びっくりする", "new", null],
    ["いう", "new", null],
    ["する", "new", null],
    ["くる", "new", null],
    ["みる", "new", null],
    ["いい", "new", null],
    ["あう", "new", null],
    ["こする", "new", null],
  ]);
  const kinds = (stem) => (index.stems.get(stem) || []).map((item) => `${item.entry.word}:${item.kind}`);
  assert.deepEqual(kinds("かけ"), ["かける:ru"]);
  assert.deepEqual(kinds("つく"), ["つくる:ru"]);
  assert.deepEqual(kinds("しま"), ["しまう:う"]);
  assert.deepEqual(kinds("もら"), ["もらう:う"]);
  assert.deepEqual(kinds("おいし"), ["おいしい:i-adj"]);
  assert.deepEqual(kinds("すご"), ["すごい:i-adj"]);
  assert.deepEqual(kinds("びっくり"), ["びっくりする:suru"]);
  // こする is a る verb with two kana to spare, not a する verb with one.
  assert.deepEqual(kinds("こす"), ["こする:ru"]);
  // A stem of one kana would be found at the start of every word beginning with it.
  for (const stem of ["い", "す", "く", "み", "あ", "こ"]) assert.deepEqual(kinds(stem), []);
  assert.equal(index.stems.size, 8);
  assert.equal(index.maxStemLen, 4);
  assert.equal(index.exact.get("かける").bounded, true);
});

test("buildIndex keeps two words that share a stem", () => {
  const index = buildIndex([["帰る", "new", null], ["帰す", "learned", null]]);
  assert.deepEqual(index.stems.get("帰").map((item) => item.kind), ["ru", "す"]);
});

test("buildIndex takes the known words as learned over their cards, the card's pitch kept", () => {
  const index = buildIndex([["猫", "new", "atamadaka"], ["犬", "suspended", null], ["鳥", null, "heiban"]], ["猫", " 犬 ", "鳥", "馬", "", "は", "a".repeat(41)]);
  assert.deepEqual(index.exact.get("猫"), { word: "猫", status: "learned", pitch: "atamadaka", bounded: false });
  assert.deepEqual(index.exact.get("犬"), { word: "犬", status: "learned", pitch: null, bounded: false });
  assert.deepEqual(index.exact.get("鳥"), { word: "鳥", status: "learned", pitch: "heiban", bounded: false });
  // A known word without a card is an entry of its own; a particle, an empty or overlong one is
  // none.
  assert.deepEqual(index.exact.get("馬"), { word: "馬", status: "learned", pitch: null, bounded: false });
  assert.equal(index.size, 4);
  // Stems for a known word, and a kana one bounded like any.
  const known = buildIndex([], ["走る", "はしる"]);
  assert.deepEqual(known.stems.get("走").map((item) => `${item.entry.word}:${item.kind}`), ["走る:ru"]);
  assert.equal(known.exact.get("はしる").bounded, true);
  // No list, or no array, changes nothing.
  assert.equal(buildIndex([["猫", "new", null]], "猫").exact.get("猫").status, "new");
  assert.equal(buildIndex([["猫", "new", null]]).exact.get("猫").status, "new");
});

// ------------------------------------------------------------------ wordStarts

test("wordStarts follows the segmenter's word boundaries", () => {
  assert.deepEqual([...wordStarts("これは日本語の字幕です")].sort((a, b) => a - b), [0, 2, 3, 6, 7, 9]);
  assert.deepEqual([...wordStarts("")], [0]);
  assert.deepEqual([...wordStarts(null)], [0]);
  assert.ok(wordStarts("猫").has(0));
});

// ------------------------------------------------------------------ markWords

test("markWords marks the deck words of a line and joins the rest", () => {
  const deck = [["日本語", "learned", null], ["字幕", "new", "heiban"]];
  assert.equal(mark("これは日本語の字幕です", deck), "これは | 日本語(learned,null) | の | 字幕(new,heiban) | です");
  assert.equal(mark("これは日本語の字幕です", deck, new Set([0, 2, 3, 6, 7, 9])), "これは | 日本語(learned,null) | の | 字幕(new,heiban) | です");
  const runs = markWords("これは日本語の字幕です", buildIndex(deck));
  assert.deepEqual(runs[0], { text: "これは", status: null, pitch: null });
  assert.deepEqual(runs[1], { text: "日本語", status: "learned", pitch: null });
  assert.equal(runs.map((run) => run.text).join(""), "これは日本語の字幕です");
});

test("markWords returns one run for text without matches and none for no text", () => {
  // Latin text is a name now ("abc" is one, below), so the text without matches is kana.
  assert.equal(mark("すごいね", []), "すごいね");
  assert.deepEqual(markWords("すごいね", buildIndex([])), [{ text: "すごいね", status: null, pitch: null }]);
  assert.deepEqual(markWords("すごいね", null), [{ text: "すごいね", status: null, pitch: null }]);
  assert.deepEqual(markWords("", buildIndex([["a", "new", null]])), []);
  assert.deepEqual(markWords(null, buildIndex([["a", "new", null]])), []);
  assert.equal(mark("猫が好き", [["犬", "new", null]]), "猫が好き");
});

test("markWords finds an ichidan verb in its conjugations, tails greedy", () => {
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べました", deck), "食べました(learned,null)");
  assert.equal(mark("食べたことがある", deck), "食べた(learned,null) | ことがある");
  assert.equal(mark("食べる", deck), "食べる(learned,null)");
  assert.equal(mark("食べない", deck), "食べない(learned,null)");
  assert.equal(mark("食べられない", deck), "食べられない(learned,null)");
  assert.equal(mark("食べたいです", deck), "食べたいです(learned,null)");
  assert.equal(mark("食べ物", deck), "食べ物");
});

test("markWords keeps the る of the potential and causative forms", () => {
  assert.equal(mark("行ける", [["行く", "learned", null]]), "行ける(learned,null)");
  assert.equal(mark("行けるよ", [["行く", "learned", null]]), "行ける(learned,null) | よ");
  assert.equal(mark("行けます", [["行く", "learned", null]]), "行けます(learned,null)");
  assert.equal(mark("行けた", [["行く", "learned", null]]), "行けた(learned,null)");
  assert.equal(mark("話せる", [["話す", "learned", null]]), "話せる(learned,null)");
  assert.equal(mark("会えるかな", [["会う", "learned", null]]), "会える(learned,null) | かな");
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べれる", deck), "食べれる(learned,null)");
  assert.equal(mark("食べさせる", deck), "食べさせる(learned,null)");
  assert.equal(mark("食べさせられた", deck), "食べさせられた(learned,null)");
  assert.equal(mark("食べてる", deck), "食べてる(learned,null)");
  assert.equal(mark("見れる", [["見る", "learned", null]]), "見れる(learned,null)");
  assert.equal(mark("来れる", [["来る", "learned", null]]), "来れる(learned,null)");
});

test("markWords finds the ichidan forms whose first piece the godan rows reach through a tail", () => {
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べたら", deck), "食べたら(learned,null)");
  assert.equal(mark("見たり見なかったり", [["見る", "learned", null]]), "見たり(learned,null) | 見なかったり(learned,null)");
  assert.equal(mark("食べましょう", deck), "食べましょう(learned,null)");
  assert.equal(mark("面白いでしょう", [["面白い", "learned", null]]), "面白いでしょう(learned,null)");
  assert.equal(mark("食べたでしょう", deck), "食べたでしょう(learned,null)");
  assert.equal(mark("食べるでしょう", deck), "食べるでしょう(learned,null)");
  assert.equal(mark("食べたくない", deck), "食べたくない(learned,null)");
  assert.equal(mark("食べたかった", deck), "食べたかった(learned,null)");
  assert.equal(mark("食べても", deck), "食べても(learned,null)");
  assert.equal(mark("食べなくて", deck), "食べなくて(learned,null)");
  assert.equal(mark("食べながら", deck), "食べながら(learned,null)");
  assert.equal(mark("食べなさい", deck), "食べなさい(learned,null)");
  assert.equal(mark("食べちゃう", deck), "食べちゃう(learned,null)");
  assert.equal(mark("食べそうだ", deck), "食べそうだ(learned,null)");
  assert.equal(mark("勉強すれば", [["勉強する", "learned", null]]), "勉強すれば(learned,null)");
});

test("markWords stops a form before the word that begins with a tail's kana", () => {
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べるだけ", deck), "食べる(learned,null) | だけ");
  assert.equal(mark("食べただけ", deck), "食べた(learned,null) | だけ");
  assert.equal(mark("食べるために", deck), "食べる(learned,null) | ために");
  assert.equal(mark("食べたため", deck), "食べた(learned,null) | ため");
  assert.equal(mark("食べるたびに", deck), "食べる(learned,null) | たびに");
  assert.equal(mark("食べたうえで", deck), "食べた(learned,null) | うえで");
  assert.equal(mark("食べてばかり", deck), "食べて(learned,null) | ばかり");
  assert.equal(mark("食べたばかり", deck), "食べた(learned,null) | ばかり");
  assert.equal(mark("食べてずっと", deck), "食べて(learned,null) | ずっと");
  assert.equal(mark("食べてうれしい", deck), "食べて(learned,null) | うれしい");
  assert.equal(mark("食べたせいで", deck), "食べた(learned,null) | せいで");
  assert.equal(mark("食べたてんぷら", deck), "食べた(learned,null) | てんぷら");
  assert.equal(mark("食べていい", deck), "食べて(learned,null) | いい");
  assert.equal(mark("食べてもいい", deck), "食べても(learned,null) | いい");
  assert.equal(mark("食べたいけど", deck), "食べたい(learned,null) | けど");
  assert.equal(mark("高いうちに", [["高い", "learned", null]]), "高い(learned,null) | うちに");
  assert.equal(mark("高いだけ", [["高い", "learned", null]]), "高い(learned,null) | だけ");
  assert.equal(mark("見ていいよ", [["見る", "learned", null]]), "見て(learned,null) | いいよ");
  assert.equal(mark("書いていい", [["書く", "learned", null]]), "書いて(learned,null) | いい");
  // The pieces that do follow: だ after ん, た after まし and て, う after the o-row, and so on,
  // whatever the segmenter makes of the line (ICU cuts 食|べた|いと|思う and 食|べ|てい|たよ).
  assert.equal(mark("食べたんだけど", deck), "食べたんだ(learned,null) | けど");
  assert.equal(mark("食べました", deck), "食べました(learned,null)");
  assert.equal(mark("食べましたよ", deck), "食べました(learned,null) | よ");
  assert.equal(mark("食べたいです", deck), "食べたいです(learned,null)");
  assert.equal(mark("食べたいと思う", deck), "食べたい(learned,null) | と思う");
  assert.equal(mark("食べていた", deck), "食べていた(learned,null)");
  assert.equal(mark("食べていたよ", deck), "食べていた(learned,null) | よ");
  assert.equal(mark("食べていたでしょう", deck), "食べていたでしょう(learned,null)");
  assert.equal(mark("食べていられない", deck), "食べていられない(learned,null)");
  assert.equal(mark("食べていました", deck), "食べていました(learned,null)");
  assert.equal(mark("食べていれば", deck), "食べていれば(learned,null)");
  assert.equal(mark("食べてた", deck), "食べてた(learned,null)");
  assert.equal(mark("行こうか", [["行く", "learned", null]]), "行こう(learned,null) | か");
  assert.equal(mark("書かれれば", [["書く", "learned", null]]), "書かれれば(learned,null)");
  assert.equal(mark("勉強させられた", [["勉強する", "learned", null]]), "勉強させられた(learned,null)");
  // Without a segmenter every index is a boundary, and the answer is the same.
  assert.equal(mark("食べるだけ", deck, [0, 1, 2, 3, 4]), "食べる(learned,null) | だけ");
  assert.equal(mark("食べたいと思う", deck, [0, 1, 2, 3, 4, 5, 6]), "食べたい(learned,null) | と思う");
});

test("markWords follows ていく as it follows ている", () => {
  assert.equal(mark("生きていく", [["生きる", "new", null]]), "生きていく(new,null)");
  assert.equal(mark("生きていける", [["生きる", "new", null]]), "生きていける(new,null)");
  assert.equal(mark("生きていかない", [["生きる", "new", null]]), "生きていかない(new,null)");
  assert.equal(mark("見ていこう", [["見る", "new", null]]), "見ていこう(new,null)");
  const deck = [["食べる", "new", null]];
  assert.equal(mark("食べていきます", deck), "食べていきます(new,null)");
  assert.equal(mark("食べていって", deck), "食べていって(new,null)");
  assert.equal(mark("頑張っていきます", [["頑張る", "new", null]]), "頑張っていきます(new,null)");
  assert.equal(mark("泳いでいく", [["泳ぐ", "new", null]]), "泳いでいく(new,null)");
  assert.equal(mark("泳いでいる", [["泳ぐ", "new", null]]), "泳いでいる(new,null)");
  assert.equal(mark("食べていた", deck), "食べていた(new,null)");
  // The stem of いる or いく alone ends no span: 見ていかが is 見て and いかが.
  assert.equal(mark("食べていかがですか", deck), "食べて(new,null) | いかがですか");
  assert.equal(mark("食べてい", deck), "食べて(new,null) | い");
});

test("markWords ends a form before もらう and follows いただく to its end", () => {
  // て + も is the concessive ても only until ら follows: 〜てもらう is another verb.
  assert.equal(mark("食べてもらう", [["食べる", "learned", null]]), "食べて(learned,null) | もらう");
  assert.equal(mark("教えてもらえますか", [["教える", "learned", null]]), "教えて(learned,null) | もらえますか");
  assert.equal(mark("書いてもらった", [["書く", "learned", null]]), "書いて(learned,null) | もらった");
  assert.equal(mark("読んでもらえる", [["読む", "learned", null]]), "読んで(learned,null) | もらえる");
  assert.equal(mark("勉強してもらう", [["勉強する", "learned", null]]), "勉強して(learned,null) | もらう");
  assert.equal(mark("食べさせてもらう", [["食べる", "learned", null]]), "食べさせて(learned,null) | もらう");
  assert.equal(mark("食べてもいい", [["食べる", "learned", null]]), "食べても(learned,null) | いい");
  assert.equal(mark("食べても", [["食べる", "learned", null]]), "食べても(learned,null)");
  // 〜ていただく is a tail of its own, and where it is not followed the span never ends inside it.
  assert.equal(mark("食べていただく", [["食べる", "learned", null]]), "食べていただく(learned,null)");
  assert.equal(mark("教えていただけますか", [["教える", "learned", null]]), "教えていただけます(learned,null) | か");
  assert.equal(mark("教えていただけませんか", [["教える", "learned", null]]), "教えていただけません(learned,null) | か");
  assert.equal(mark("見ていただいて", [["見る", "learned", null]]), "見ていただいて(learned,null)");
  assert.equal(mark("読んでいただきました", [["読む", "learned", null]]), "読んでいただきました(learned,null)");
  assert.equal(mark("使わせていただきました", [["使う", "learned", null]]), "使わせていただきました(learned,null)");
  assert.equal(mark("食べていただければ", [["食べる", "learned", null]]), "食べていただければ(learned,null)");
  assert.equal(mark("食べていただこう", [["食べる", "learned", null]]), "食べていただこう(learned,null)");
  assert.equal(mark("食べていただかないと", [["食べる", "learned", null]]), "食べていただかない(learned,null) | と");
  assert.equal(mark("食べていただ", [["食べる", "learned", null]]), "食べて(learned,null) | いただ");
  assert.equal(mark("食べていた", [["食べる", "learned", null]]), "食べていた(learned,null)");
  assert.equal(mark("食べていたよ", [["食べる", "learned", null]]), "食べていた(learned,null) | よ");
  assert.equal(mark("書いただけ", [["書く", "learned", null]]), "書いた(learned,null) | だけ");
  assert.equal(mark("食べていただく", [["食べる", "learned", null]], [0, 1, 2, 3, 4, 5, 6]), "食べていただく(learned,null)");
  assert.equal(mark("食べてもらう", [["食べる", "learned", null]], [0, 1, 2, 3, 4, 5]), "食べて(learned,null) | もらう");
});

test("markWords needs a tail after a piece that is no form on its own", () => {
  // A godan verb's a-row and o-row: 行か needs ない, 行こ needs う.
  assert.equal(mark("聞こえる", [["聞く", "learned", null]]), "聞こえる");
  assert.equal(mark("聞こえた", [["聞く", "learned", null]]), "聞こえた");
  assert.equal(mark("死の恐怖", [["死ぬ", "learned", null]]), "死の恐怖");
  assert.equal(mark("死のゲーム", [["死ぬ", "learned", null]]), "死のゲーム");
  assert.equal(mark("聞こう", [["聞く", "learned", null]]), "聞こう(learned,null)");
  assert.equal(mark("死のう", [["死ぬ", "learned", null]]), "死のう(learned,null)");
  assert.equal(mark("行こうか", [["行く", "learned", null]]), "行こう(learned,null) | か");
  assert.equal(mark("行こっか", [["行く", "learned", null]]), "行こっ(learned,null) | か");
  assert.equal(mark("行こー", [["行く", "learned", null]]), "行こー(learned,null)");
  assert.equal(mark("行かなきゃ", [["行く", "learned", null]]), "行かなきゃ(learned,null)");
  assert.equal(mark("読まなきゃ", [["読む", "learned", null]]), "読まなきゃ(learned,null)");
  assert.equal(mark("帰らなきゃ", [["帰る", "learned", null]]), "帰らなきゃ(learned,null)");
  assert.equal(mark("食べなきゃ", [["食べる", "learned", null]]), "食べなきゃ(learned,null)");
  assert.equal(mark("食べなきゃだめ", [["食べる", "learned", null]]), "食べなきゃ(learned,null) | だめ");
  assert.equal(mark("勉強しなきゃ", [["勉強する", "learned", null]]), "勉強しなきゃ(learned,null)");
  assert.equal(mark("高くなきゃ", [["高い", "learned", null]]), "高くなきゃ(learned,null)");
  assert.equal(mark("行かねば", [["行く", "learned", null]]), "行かねば");
  assert.equal(mark("行かず", [["行く", "learned", null]]), "行かず(learned,null)");
  assert.equal(mark("帰らん", [["帰る", "learned", null]]), "帰らん(learned,null)");
  // An adjective's く-form is a deck entry of its own (多く): 多かった is not a form of it.
  assert.equal(mark("多かった", [["多く", "learned", null]]), "多かった");
  assert.equal(mark("多くない", [["多い", "learned", null]]), "多くない(learned,null)");
  // A する verb's さ, せ and す, unlike an adjective's さ.
  assert.equal(mark("電話さえ", [["電話する", "learned", null]]), "電話(learned,null) | さえ");
  assert.equal(mark("電話さえすれば", [["電話する", "learned", null]]), "電話(learned,null) | さえすれば");
  assert.equal(mark("勉強すら", [["勉強する", "learned", null]]), "勉強(learned,null) | すら");
  assert.equal(mark("勉強すべき", [["勉強する", "learned", null]]), "勉強(learned,null) | すべき");
  assert.equal(mark("勉強させる", [["勉強する", "learned", null]]), "勉強させる(learned,null)");
  assert.equal(mark("勉強される", [["勉強する", "learned", null]]), "勉強される(learned,null)");
  assert.equal(mark("勉強せず", [["勉強する", "learned", null]]), "勉強せず(learned,null)");
  assert.equal(mark("勉強せよ", [["勉強する", "learned", null]]), "勉強せよ(learned,null)");
  assert.equal(mark("勉強すれば", [["勉強する", "learned", null]]), "勉強すれば(learned,null)");
  assert.equal(mark("高さが", [["高い", "learned", null]]), "高さ(learned,null) | が");
  // けれ only reaches ければ, まし only ました.
  assert.equal(mark("食べるけれど", [["食べる", "learned", null]]), "食べる(learned,null) | けれど");
  assert.equal(mark("高いけれど", [["高い", "learned", null]]), "高い(learned,null) | けれど");
  assert.equal(mark("行ったけれども", [["行く", "learned", null]]), "行った(learned,null) | けれども");
  assert.equal(mark("食べたければ", [["食べる", "learned", null]]), "食べたければ(learned,null)");
  assert.equal(mark("高ければ", [["高い", "learned", null]]), "高ければ(learned,null)");
  // まし alone is no form, but 食べ is (the noun form), so a line cut there keeps the stem.
  assert.equal(mark("食べまし", [["食べる", "learned", null]]), "食べ(learned,null) | まし");
  assert.equal(mark("食べまして", [["食べる", "learned", null]]), "食べまして(learned,null)");
  assert.equal(mark("帰りまし", [["帰る", "learned", null]]), "帰り(learned,null) | まし");
  assert.equal(mark("聞こえる", [["聞く", "learned", null]], [0, 1, 2, 3]), "聞こえる");
  assert.equal(mark("電話さえ", [["電話する", "learned", null]], [0, 1, 2, 3]), "電話(learned,null) | さえ");
});

test("markWords ends a form before the word its last piece would begin", () => {
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べたらしい", deck), "食べた(learned,null) | らしい");
  assert.equal(mark("食べたらしくない", deck), "食べた(learned,null) | らしくない");
  assert.equal(mark("行ったらしい", [["行く", "learned", null]]), "行った(learned,null) | らしい");
  assert.equal(mark("死んだらしい", [["死ぬ", "learned", null]]), "死んだ(learned,null) | らしい");
  assert.equal(mark("勉強したらしい", [["勉強する", "learned", null]]), "勉強した(learned,null) | らしい");
  assert.equal(mark("食べたら", deck), "食べたら(learned,null)");
  assert.equal(mark("食べたらね", deck), "食べたら(learned,null) | ね");
  assert.equal(mark("食べたらしばらく休む", deck), "食べたら(learned,null) | しばらく休む");
  assert.equal(mark("食べたくせに", deck), "食べた(learned,null) | くせに");
  assert.equal(mark("知ってたくせに", [["知る", "learned", null]]), "知ってた(learned,null) | くせに");
  assert.equal(mark("高かったくせに", [["高い", "learned", null]]), "高かった(learned,null) | くせに");
  assert.equal(mark("食べてたくさん寝た", deck), "食べて(learned,null) | たくさん寝た");
  assert.equal(mark("行ってたくさん食べた", [["行く", "learned", null]]), "行って(learned,null) | たくさん食べた");
  assert.equal(mark("高いたくさん", [["高い", "learned", null]]), "高い(learned,null) | たくさん");
  assert.equal(mark("食べたくらい", deck), "食べた(learned,null) | くらい");
  assert.equal(mark("食べたくて", deck), "食べたくて(learned,null)");
  assert.equal(mark("食べたくなる", deck), "食べたく(learned,null) | なる");
  assert.equal(mark("見ていきなり", [["見る", "learned", null]]), "見て(learned,null) | いきなり");
  assert.equal(mark("行っていきなり", [["行く", "learned", null]]), "行って(learned,null) | いきなり");
  assert.equal(mark("食べていきなり倒れた", deck), "食べて(learned,null) | いきなり倒れた");
  assert.equal(mark("買っていくらだった", [["買う", "learned", null]]), "買って(learned,null) | いくらだった");
  assert.equal(mark("食べていきます", deck), "食べていきます(learned,null)");
  assert.equal(mark("生きていきなさい", [["生きる", "learned", null]]), "生きていきなさい(learned,null)");
  assert.equal(mark("食べたらしい", deck, [0, 1, 2, 3, 4, 5]), "食べた(learned,null) | らしい");
  assert.equal(mark("見ていきなり", [["見る", "learned", null]], [0, 1, 2, 3, 4, 5]), "見て(learned,null) | いきなり");
});

test("markWords lets ん follow the forms it shortens and not ちゃ", () => {
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べてちゃんと寝て", deck), "食べてちゃ(learned,null) | んと寝て");
  assert.equal(mark("見てちゃんと", [["見る", "learned", null]]), "見てちゃ(learned,null) | んと");
  assert.equal(mark("聞いてちゃんと答えろ", [["聞く", "learned", null]]), "聞いてちゃ(learned,null) | んと答えろ");
  assert.equal(mark("食べるじゃん", deck), "食べるじゃん(learned,null)");
  assert.equal(mark("食べません", deck), "食べません(learned,null)");
  assert.equal(mark("食べますんで", deck), "食べますんで(learned,null)");
  assert.equal(mark("行かん", [["行く", "learned", null]]), "行かん(learned,null)");
  assert.equal(mark("食べてんの", deck), "食べてん(learned,null) | の");
  assert.equal(mark("食べるんじゃない", deck), "食べるんじゃない(learned,null)");
  assert.equal(mark("食べたんだけど", deck), "食べたんだ(learned,null) | けど");
  // ICU cuts 勉強|せん|とい|て: the と of the contracted ておいて takes the form's colour like a
  // particle (it belongs to the form), and いて after it is いる to the matcher.
  assert.equal(mark("勉強せんといて", [["勉強する", "learned", null]]), "勉強せん(learned,null) | といて");
  assert.equal(mark("勉強すんの", [["勉強する", "learned", null]]), "勉強すん(learned,null) | の");
});

test("markWords does not end a span inside a compound", () => {
  assert.equal(mark("関係ないよ", [["関する", "learned", null]]), "関係ないよ");
  // 関東 is a place name, blue by rule; 関 is still not 関する's.
  assert.equal(mark("関東地方", [["関する", "learned", null]]), "関東(proper,null) | 地方");
  assert.equal(mark("対応します", [["対する", "learned", null]]), "対応します");
  assert.equal(mark("結婚式", [["結婚する", "learned", null]]), "結婚式");
  assert.equal(mark("飲み物", [["飲む", "learned", null]]), "飲み物");
  assert.equal(mark("買い物", [["買う", "learned", null]]), "買い物");
  assert.equal(mark("行き先", [["行く", "learned", null]]), "行き先");
  assert.equal(mark("日本語", [["日本", "learned", null]], new Set([0])), "日本語");
  assert.equal(mark("日本語", [["日本", "learned", null]]), "日本語");
  assert.equal(mark("コーヒー", [["コーヒ", "learned", null]], new Set([0])), "コーヒー");
  // A boundary there, or a kana after it, makes it an end.
  assert.equal(mark("見た犬", [["見る", "learned", null]]), "見た(learned,null) | 犬");
  assert.equal(mark("食べた後", [["食べる", "learned", null]]), "食べた(learned,null) | 後");
  assert.equal(mark("電話番号", [["電話する", "learned", null]]), "電話(learned,null) | 番号");
  assert.equal(mark("日本語", [["日本", "learned", null]], new Set([0, 2])), "日本(learned,null) | 語");
  assert.equal(mark("関して", [["関する", "learned", null]]), "関して(learned,null)");
});

test("markWords needs the first piece: a bare stem is another word", () => {
  const deck = [["走る", "learned", null]];
  assert.equal(mark("走った", deck), "走った(learned,null)");
  assert.equal(mark("走者", deck), "走者");
  assert.equal(mark("走ります", deck), "走ります(learned,null)");
});

test("markWords never colours a particle by a card of its own, whatever the deck holds", () => {
  // Yomitan looks particles up like any word, so a deck can hold は, の or のは; painting the は of
  // every line would say nothing about the viewer's words. A particle takes the colour of the
  // word before it, and nothing else (のは after 私, never a pitch).
  const deck = [["の", "learned", null], ["は", "new", null], ["のは", "learned", "heiban"], ["私", "learned", null]];
  assert.equal(mark("私のは赤い", deck), "私(learned,null) | のは赤い");
  assert.equal(mark("日本の首都", [["の", "learned", null]]), "日本(proper,null) | の首都");
  assert.equal(mark("これは本です", [["は", "new", null]]), "これは本です");
  assert.equal(mark("行くから", [["から", "learned", null], ["行く", "new", null]]), "行く(new,null) | から");
  assert.equal(buildIndex([["は", "new", null], ["のに", "new", null], ["でも", "new", null]]).size, 0);
  // A particle inside a longer word is still that word.
  assert.equal(mark("彼のはなし", [["はなし", "learned", null]]), "彼の | はなし(learned,null)");
  // The の fusions and かも, which ICU cuts as words of their own, are particles too: かも takes
  // 行く's colour, and the し of しれない, which ICU cuts into し|れ|ない, is no particle here.
  assert.equal(mark("行くかもしれない", [["かも", "new", null], ["行く", "learned", null]]), "行く(learned,null) | かもしれない");
  assert.equal(mark("東京への旅", [["への", "new", null]]), "東京(proper,null) | への旅");
  assert.equal(mark("彼との約束", [["との", "new", null]]), "彼との約束");
  assert.equal(mark("家での生活", [["での", "new", null]]), "家での生活");
  assert.equal(buildIndex([["かも", "new", null], ["への", "new", null], ["との", "new", null], ["での", "new", null]]).size, 0);
});

test("markWords never colours the copula or an auxiliary, whatever the deck holds", () => {
  // です, だ, ます, ない or ん end nearly every line: a card for one of them (JMdict has them all)
  // would paint every line the same way a card for は would.
  assert.equal(mark("これは日本語の字幕です", [["です", "new", null]]), "これは日本語の字幕です");
  assert.equal(mark("いいですね", [["です", "new", null]]), "いいですね");
  assert.equal(mark("それは猫だ", [["だ", "new", null]]), "それは猫だ");
  assert.equal(mark("分からない", [["ない", "new", null]]), "分からない");
  assert.equal(mark("雨かもしれない", [["ない", "new", null], ["かも", "new", null]]), "雨かもしれない");
  assert.equal(mark("食べます", [["ます", "new", null]]), "食べます");
  assert.equal(mark("行くんだもん", [["ん", "new", null], ["もん", "new", null]]), "行くんだもん");
  assert.equal(mark("それじゃない", [["じゃ", "new", null]]), "それじゃない");
  assert.equal(mark("行くでしょう", [["でしょう", "new", null]]), "行くでしょう");
  assert.equal(mark("食べたい", [["たい", "new", null]]), "食べたい");
  const copula = ["だ", "だった", "だろう", "だろ", "です", "でした", "でしょう", "でしょ", "ます", "ません", "ました", "ない", "たい", "ん", "じゃ", "じゃん", "かも", "もん"];
  assert.equal(buildIndex(copula.map((word) => [word, "new", null])).size, 0);
  // The forms themselves are still found through the tables, and a kanji word is not a particle.
  assert.equal(mark("分からない", [["ない", "new", null], ["分かる", "learned", null]]), "分からない(learned,null)");
  assert.equal(mark("無い", [["無い", "new", null]]), "無い(new,null)");
});

test("markWords takes a verb's noun form as the verb", () => {
  // A godan verb's noun form is its stem plus the i-row piece; an ichidan verb's is the bare
  // stem, which ends in kana. A bare stem ending in a kanji stays another word.
  assert.equal(mark("終わりが来た", [["終わる", "learned", null]]), "終わり(learned,null) | が来た");
  assert.equal(mark("話しをした", [["話す", "learned", null]]), "話し(learned,null) | をした");
  assert.equal(mark("動きが速い", [["動く", "learned", null]]), "動き(learned,null) | が速い");
  assert.equal(mark("食べに行く", [["食べる", "learned", null]]), "食べ(learned,null) | に行く");
  assert.equal(mark("助けを呼ぶ", [["助ける", "new", null]]), "助け(new,null) | を呼ぶ");
  assert.equal(mark("流れが変わる", [["流れる", "learned", null]]), "流れ(learned,null) | が変わる");
  assert.equal(mark("考えは", [["考える", "learned", null]]), "考え(learned,null) | は");
  // A compound is not the verb (the span may not end before a kanji), and a kanji stem is not a form.
  assert.equal(mark("食べ物", [["食べる", "learned", null]]), "食べ物");
  assert.equal(mark("考え方", [["考える", "learned", null]]), "考え方");
  assert.equal(mark("走者", [["走る", "learned", null]]), "走者");
  assert.equal(mark("見物", [["見る", "learned", null]]), "見物");
  // The conjugated forms still win over the bare stem.
  assert.equal(mark("食べました", [["食べる", "learned", null]]), "食べました(learned,null)");
  assert.equal(mark("終わった", [["終わる", "learned", null]]), "終わった(learned,null)");
});

test("markWords grants the bare stem to an ichidan verb at a word boundary only", () => {
  // A godan る verb's stem ends in the a-row and is no form: ICU keeps 当たり前 in one segment,
  // so the り form may not end before 前, and the bare 当た must not step in for it.
  assert.equal(mark("当たり前だよ", [["当たる", "learned", null]], new Set([0, 4, 5])), "当たり前だよ");
  assert.equal(mark("当たり前だよ", [["当たる", "learned", null]]), "当たり前だよ");
  assert.equal(mark("変わり者", [["変わる", "learned", null]], new Set([0])), "変わり者");
  assert.equal(mark("曲がり角", [["曲がる", "learned", null]], new Set([0])), "曲がり角");
  assert.equal(mark("代わり映え", [["代わる", "learned", null]], new Set([0])), "代わり映え");
  assert.equal(mark("分かち合おう", [["分かる", "learned", null]]), "分かち合おう");
  assert.equal(mark("転がして", [["転がる", "learned", null]]), "転がして");
  assert.equal(mark("散らかして", [["散らかる", "learned", null]]), "散らかして");
  // An ichidan stem inside a compound ICU holds together is not the noun form.
  assert.equal(mark("見せかけ", [["見せる", "learned", null]], new Set([0])), "見せかけ");
  assert.equal(mark("生きがい", [["生きる", "learned", null]], new Set([0])), "生きがい");
  assert.equal(mark("混じり気", [["混じる", "learned", null]], new Set([0])), "混じり気");
  assert.equal(mark("見せかけ", [["見せる", "learned", null]]), "見せかけ");
  // At a boundary the noun form stands, and the conjugations are untouched.
  assert.equal(mark("見せかけ", [["見せる", "learned", null]], new Set([0, 2])), "見せ(learned,null) | かけ");
  assert.equal(mark("食べに行く", [["食べる", "learned", null]]), "食べ(learned,null) | に行く");
  assert.equal(mark("起きに", [["起きる", "learned", null]]), "起き(learned,null) | に");
  assert.equal(mark("当たりが出た", [["当たる", "learned", null]]), "当たり(learned,null) | が出た");
  assert.equal(mark("当たった", [["当たる", "learned", null]]), "当たった(learned,null)");
  assert.equal(mark("分かります", [["分かる", "learned", null]]), "分かります(learned,null)");
  assert.equal(mark("転がった", [["転がる", "learned", null]]), "転がった(learned,null)");
  assert.equal(mark("混じって", [["混じる", "learned", null]]), "混じって(learned,null)");
  // Without a segmenter every index is a boundary, and the row rule alone keeps the godan stems out.
  assert.equal(mark("当たり前だよ", [["当たる", "learned", null]], [0, 1, 2, 3, 4, 5]), "当たり(learned,null) | 前だよ");
  assert.equal(mark("転がして", [["転がる", "learned", null]], [0, 1, 2, 3]), "転がして");
  assert.equal(mark("分かち合おう", [["分かる", "learned", null]], [0, 1, 2, 3, 4]), "分かち合おう");
});

test("markWords never ends a span right after the 音便 kana", () => {
  // 行い is 行う's noun and 行います its polite form; 引っかかる and 追っかける are verbs of their
  // own: い, っ and ん before た or て are no form alone and reach nothing but た and て.
  const go = [["行く", "learned", null]];
  assert.equal(mark("会議を行います", go), "会議を行います");
  assert.equal(mark("彼の行いが悪い", go), "彼の行いが悪い");
  assert.equal(mark("行いました", go), "行いました");
  assert.equal(mark("行いたい", go), "行いたい");
  assert.equal(mark("彼の行いです", go), "彼の行いです");
  assert.equal(mark("引っかかった", [["引く", "learned", null]]), "引っかかった");
  assert.equal(mark("引っかけて", [["引く", "learned", null]]), "引っかけて");
  assert.equal(mark("追っかけ", [["追う", "learned", null]]), "追っかけ");
  assert.equal(mark("取っかかり", [["取る", "learned", null]]), "取っかかり");
  assert.equal(mark("飲んどけ", [["飲む", "learned", null]]), "飲んどけ");
  // The forms themselves, and 行う's own.
  assert.equal(mark("行った", go), "行った(learned,null)");
  assert.equal(mark("行って", go), "行って(learned,null)");
  assert.equal(mark("行ったら", go), "行ったら(learned,null)");
  assert.equal(mark("行きます", go), "行きます(learned,null)");
  assert.equal(mark("書いちゃう", [["書く", "learned", null]]), "書いちゃう(learned,null)");
  assert.equal(mark("書いとく", [["書く", "learned", null]]), "書いとく(learned,null)");
  assert.equal(mark("書いたいろいろ", [["書く", "learned", null]]), "書いた(learned,null) | いろいろ");
  assert.equal(mark("飲んでも", [["飲む", "learned", null]]), "飲んでも(learned,null)");
  assert.equal(mark("飲んでる", [["飲む", "learned", null]]), "飲んでる(learned,null)");
  assert.equal(mark("取った", [["取る", "learned", null]]), "取った(learned,null)");
  assert.equal(mark("行います", [["行う", "new", null]]), "行います(new,null)");
  assert.equal(mark("行いたい", [["行う", "new", null], ["行く", "learned", null]]), "行いたい(new,null)");
  assert.equal(mark("行いが悪い", [["行う", "new", null]]), "行い(new,null) | が悪い");
  // A す verb's し is a form alone (the noun) and takes ます, while it takes た and て like the 音便 kana.
  assert.equal(mark("話しをした", [["話す", "learned", null]]), "話し(learned,null) | をした");
  assert.equal(mark("話した", [["話す", "learned", null]]), "話した(learned,null)");
  assert.equal(mark("話して", [["話す", "learned", null]]), "話して(learned,null)");
  assert.equal(mark("話します", [["話す", "learned", null]]), "話します(learned,null)");
  assert.equal(mark("話したい", [["話す", "learned", null]]), "話したい(learned,null)");
  assert.equal(mark("会議を行います", go, [0, 1, 2, 3, 4, 5, 6]), "会議を行います");
  assert.equal(mark("引っかかった", [["引く", "learned", null]], [0, 1, 2, 3, 4, 5]), "引っかかった");
});

test("markWords admits the っ of a く verb after 行 alone", () => {
  // 行く is the one く verb whose 音便 is っ (行った); every other takes い (歩いた, 書いた). A kana
  // く verb whose stem is a word of its own would else be found in that word and the quotative
  // after it: ICU cuts ある|って|言|っ|た, and あるって is ある and って, not 歩く.
  const walk = [["あるく", "learned", null]];
  assert.equal(mark("あるって言った", walk), "あるって言った");
  assert.equal(mark("あるって言った", walk, new Set([0, 2, 4, 5, 6])), "あるって言った");
  assert.equal(mark("ここにあるって", walk), "ここにあるって");
  assert.equal(mark("はたらって", [["はたらく", "learned", null]]), "はたらって");
  assert.equal(mark("書って", [["書く", "learned", null]]), "書って");
  // The い forms and 行く's っ, in the word alone and in a compound ending in 行く.
  assert.equal(mark("あるいて", walk), "あるいて(learned,null)");
  assert.equal(mark("あるいた", walk), "あるいた(learned,null)");
  assert.equal(mark("書いた", [["書く", "learned", null]]), "書いた(learned,null)");
  assert.equal(mark("行った", [["行く", "learned", null]]), "行った(learned,null)");
  assert.equal(mark("連れて行った", [["連れて行く", "learned", null]]), "連れて行った(learned,null)");
});

test("markWords never ends a span right after かっ", () => {
  // かっ, なかっ and たかっ only exist before た: the かっ of かっこいい after a form is that word's.
  assert.equal(mark("あの時見たかっこいい人", [["見る", "learned", null]]), "あの時 | 見た(learned,null) | かっこいい人");
  assert.equal(mark("食べてかっこ悪い", [["食べる", "learned", null]]), "食べて(learned,null) | かっこ悪い");
  assert.equal(mark("高いかっこう", [["高い", "learned", null]]), "高い(learned,null) | かっこう");
  assert.equal(mark("高かっ", [["高い", "learned", null]]), "高かっ");
  assert.equal(mark("食べたかっ", [["食べる", "learned", null]]), "食べた(learned,null) | かっ");
  assert.equal(mark("食べたかった", [["食べる", "learned", null]]), "食べたかった(learned,null)");
  assert.equal(mark("食べたかったら", [["食べる", "learned", null]]), "食べたかったら(learned,null)");
  assert.equal(mark("食べなかった", [["食べる", "learned", null]]), "食べなかった(learned,null)");
  assert.equal(mark("高かったら", [["高い", "learned", null]]), "高かったら(learned,null)");
  assert.equal(mark("高くなかった", [["高い", "learned", null]]), "高くなかった(learned,null)");
  assert.equal(mark("書きたかった", [["書く", "learned", null]]), "書きたかった(learned,null)");
  assert.equal(mark("あの時見たかっこいい人", [["見る", "learned", null]], [...Array(11).keys()]), "あの時 | 見た(learned,null) | かっこいい人");
});

test("markWords lets よう be the volitional only after an ichidan-like piece", () => {
  // After る, た, ない, the u-row or an adjective, よう is 様: ように, ようだ, ような.
  const deck = [["食べる", "learned", null]];
  assert.equal(mark("食べるように言われた", deck), "食べる(learned,null) | ように言われた");
  assert.equal(mark("食べたような気がする", deck), "食べた(learned,null) | ような気がする");
  assert.equal(mark("食べないようにしている", deck), "食べない(learned,null) | ようにしている");
  assert.equal(mark("見るようになった", [["見る", "learned", null]]), "見る(learned,null) | ようになった");
  assert.equal(mark("行くようだ", [["行く", "learned", null]]), "行く(learned,null) | ようだ");
  assert.equal(mark("高いようで", [["高い", "learned", null]]), "高い(learned,null) | ようで");
  assert.equal(mark("勉強するように", [["勉強する", "learned", null]]), "勉強する(learned,null) | ように");
  assert.equal(mark("来るよう", [["来る", "learned", null]]), "来る(learned,null) | よう");
  // The volitional: 食べよう is a first piece, ましょう is ましょ + う, and the pieces that
  // conjugate like 食べ take よう; the potential's e-row does not (書けよう is no form).
  assert.equal(mark("食べよう", deck), "食べよう(learned,null)");
  assert.equal(mark("食べましょう", deck), "食べましょう(learned,null)");
  assert.equal(mark("食べていよう", deck), "食べていよう(learned,null)");
  assert.equal(mark("食べられよう", deck), "食べられよう(learned,null)");
  assert.equal(mark("書かせよう", [["書く", "learned", null]]), "書かせよう(learned,null)");
  assert.equal(mark("勉強しよう", [["勉強する", "learned", null]]), "勉強しよう(learned,null)");
  assert.equal(mark("書けよう", [["書く", "learned", null]]), "書け(learned,null) | よう");
  assert.equal(mark("食べるように言われた", deck, [...Array(10).keys()]), "食べる(learned,null) | ように言われた");
});

test("markWords finds an i-adjective in its forms", () => {
  const deck = [["美しい", "learned", null]];
  assert.equal(mark("美しかった", deck), "美しかった(learned,null)");
  assert.equal(mark("美しさ", deck), "美しさ(learned,null)");
  assert.equal(mark("美しくない", deck), "美しくない(learned,null)");
  assert.equal(mark("美しければ", deck), "美しければ(learned,null)");
  assert.equal(mark("美しい", deck), "美しい(learned,null)");
});

test("markWords follows the godan rows", () => {
  const write = [["書く", "learned", null]];
  assert.equal(mark("書かない", write), "書かない(learned,null)");
  assert.equal(mark("書いて", write), "書いて(learned,null)");
  assert.equal(mark("書店", write), "書店");
  assert.equal(mark("行った", [["行く", "learned", null]]), "行った(learned,null)");
  assert.equal(mark("泳いでいる", [["泳ぐ", "learned", null]]), "泳いでいる(learned,null)");
  assert.equal(mark("話します", [["話す", "learned", null]]), "話します(learned,null)");
  assert.equal(mark("待って", [["待つ", "learned", null]]), "待って(learned,null)");
  assert.equal(mark("死んだ", [["死ぬ", "learned", null]]), "死んだ(learned,null)");
  assert.equal(mark("遊んでいた", [["遊ぶ", "learned", null]]), "遊んでいた(learned,null)");
  assert.equal(mark("読みました", [["読む", "learned", null]]), "読みました(learned,null)");
  assert.equal(mark("会わなかった", [["会う", "learned", null]]), "会わなかった(learned,null)");
});

test("markWords takes a する verb with its endings or as the noun alone", () => {
  const deck = [["勉強する", "learned", null]];
  assert.equal(mark("勉強している", deck), "勉強している(learned,null)");
  assert.equal(mark("勉強が", deck), "勉強(learned,null) | が");
  assert.equal(mark("勉強しました", deck), "勉強しました(learned,null)");
  assert.equal(mark("勉強させられた", deck), "勉強させられた(learned,null)");
  // The noun alone takes no tails: 勉強です is 勉強 and です.
  assert.equal(mark("勉強です", deck), "勉強(learned,null) | です");
});

test("markWords finds 来る in 来なかった but not in 来週", () => {
  const deck = [["来る", "learned", null]];
  assert.equal(mark("来なかった", deck), "来なかった(learned,null)");
  assert.equal(mark("来週", deck), "来週");
});

test("markWords stops a hiragana word at a word boundary", () => {
  const deck = [["ある", "learned", null]];
  assert.equal(mark("あるいは", deck, new Set([0])), "あるいは");
  assert.equal(mark("ある。", deck, new Set([0, 2])), "ある(learned,null) | 。");
  assert.equal(mark("ある。", deck), "ある(learned,null) | 。");
  assert.equal(mark("ある", deck, new Set([0])), "ある(learned,null)");
  // Every index is a boundary when the caller says so.
  assert.equal(mark("あるいは", deck, new Set([0, 1, 2, 3])), "ある(learned,null) | いは");
});

test("markWords with the segmenter colours 食べる and not ある in あるいは食べる", () => {
  const deck = [["ある", "learned", null], ["食べる", "new", null]];
  assert.equal(mark("あるいは食べる", deck), "あるいは | 食べる(new,null)");
});

test("markWords lets an exact word win a tie and the longer span otherwise", () => {
  assert.equal(mark("見た", [["見", "new", null], ["見る", "learned", null]]), "見た(learned,null)");
  assert.equal(mark("見", [["見", "new", null], ["見る", "learned", null]]), "見(new,null)");
  assert.equal(mark("勉強", [["勉強", "new", null], ["勉強する", "learned", null]]), "勉強(new,null)");
  assert.equal(mark("日本語", [["日本", "new", null], ["日本語", "learned", null]]), "日本語(learned,null)");
});

test("markWords starts a match only at a word boundary", () => {
  assert.equal(mark("大学生", [["学生", "learned", null]], new Set([0])), "大学生");
  assert.equal(mark("大学生", [["大学生", "learned", null]], new Set([0])), "大学生(learned,null)");
  assert.equal(mark("大学生", [["学生", "learned", null]], new Set([0, 1])), "大 | 学生(learned,null)");
  assert.equal(mark("大学生", [["生", "learned", null]], new Set([0, 2])), "大学 | 生(learned,null)");
  // 東京都 is a place name: blue from its start whatever the deck holds inside it, and the deck's
  // own 東京都 wins the tie.
  assert.equal(mark("東京都", [["京都", "learned", null]], new Set([0, 2])), "東京都(proper,null)");
  assert.equal(mark("東京都", [["東京都", "learned", null]], new Set([0, 2])), "東京都(learned,null)");
  assert.equal(mark("東京都", [["京都", "learned", null]], new Set([0, 1])), "東京都(proper,null)");
  assert.equal(mark("東京都", [["都", "learned", null]], new Set([0, 2])), "東京都(proper,null)");
});

test("markWords accepts the boundaries as any iterable", () => {
  assert.equal(mark("大学生", [["学生", "learned", null]], [0, 1]), "大 | 学生(learned,null)");
});

test("markWords never overlaps and continues after a match", () => {
  const deck = [["猫", "new", "atamadaka"], ["犬", "learned", "nakadaka"], ["見る", "learning", null]];
  assert.equal(mark("猫と犬を見ました", deck), "猫(new,atamadaka) | と | 犬(learned,nakadaka) | を | 見ました(learning,null)");
  assert.equal(mark("猫猫", deck, new Set([0, 1])), "猫(new,atamadaka) | 猫(new,atamadaka)");
});

// The viewer's two lines, with the boundaries ICU gives them (probed with Node 24).
const BATH = "ちょうどこのお風呂の中で";
const BATH_STARTS = new Set([0, 4, 6, 7, 9, 10, 11]);
const BATH_DECK = [["ちょうど", "learned", null], ["この", "learned", null], ["風呂", "new", "heiban"], ["中", "learning", "atamadaka"]];
const CALL = "視聴者の方に話しかけていただくっていうね";
const CALL_STARTS = new Set([0, 2, 3, 4, 5, 6, 7, 8, 10, 11, 15, 19]);
const CALL_DECK = [["視聴者", "learned", null], ["方", "learned", null], ["話しかける", "new", null], ["いただく", "learning", null], ["いう", "learned", "heiban"]];

test("markWords gives the honorific prefix the colour of the word it fronts and leaves the particles plain", () => {
  // ICU cuts ちょうど|この|お|風呂|の|中|で: the お takes 風呂's status (never its pitch), since it
  // is part of the word; の and で have cards of their own or none, so the line reads
  // [ちょうどこの][お風呂][の][中][で].
  const want = "ちょうど(learned,null) | この(learned,null) | お(new,null) | 風呂(new,heiban) | の | 中(learning,atamadaka) | で";
  assert.equal(mark(BATH, BATH_DECK, BATH_STARTS), want);
  assert.equal(mark(BATH, BATH_DECK), want);
  assert.deepEqual(markWords(BATH, buildIndex(BATH_DECK), BATH_STARTS).slice(2, 5), [
    { text: "お", status: "new", pitch: null },
    { text: "風呂", status: "new", pitch: "heiban" },
    { text: "の", status: null, pitch: null },
  ]);
  assert.equal(mark(BATH, [["風呂", "new", null], ["中", "learning", null]], BATH_STARTS), "ちょうどこの | お(new,null) | 風呂(new,null) | の | 中(learning,null) | で");
  // A word with a pitch and no status has no colour to run on: its prefix is plain text like the
  // rest, joined with it.
  assert.equal(mark("お風呂", [["風呂", null, "heiban"]], new Set([0, 1])), "お | 風呂(null,heiban)");
  assert.equal(markWords("お風呂", buildIndex([["風呂", null, "heiban"]]), new Set([0, 1])).length, 2);
  assert.equal(mark("お風呂の中", [["風呂", null, "heiban"]], new Set([0, 1, 3, 4])), "お | 風呂(null,heiban) | の中");
  assert.equal(markWords("お風呂の中", buildIndex([["風呂", null, "heiban"]]), new Set([0, 1, 3, 4])).length, 3);
});

test("markWords finds the word after a quotative inside its segment", () => {
  // ICU keeps っていう in one segment, so いう never begins one: it is found after the って, which
  // takes the colour of 話しかけていただく before it, the segment being one piece. The particles
  // stay plain: [視聴者][の][方][に][話しかけていただくって][いう][ね].
  const want =
    "視聴者(learned,null) | の | 方(learned,null) | に | 話しかけていただく(new,null) | って(new,null) | いう(learned,heiban) | ね";
  assert.equal(mark(CALL, CALL_DECK, CALL_STARTS), want);
  assert.equal(mark(CALL, CALL_DECK), want);
  // The viewer's deck holds かける rather than 話しかける: its form runs to the same end.
  assert.equal(mark(CALL, [["かける", "new", null], ["いう", "learned", null]], CALL_STARTS), "視聴者の方に話し | かけていただく(new,null) | って(new,null) | いう(learned,null) | ね");
  // With nothing coloured before it, the quotative stays plain.
  assert.equal(mark(CALL, [["いう", "learned", null]], CALL_STARTS), "視聴者の方に話しかけていただくって | いう(learned,null) | ね");
  assert.equal(mark(CALL, [["いう", "learned", null]]), "視聴者の方に話しかけていただくって | いう(learned,null) | ね");
});

test("markWords tries the word after お or ご only where ICU cut the prefix off", () => {
  // お|風呂, ご|家族 and お|仕事 are cut; お茶, お前, お金, ご飯 and お母さん are words of their own,
  // so 前 in the deck never colours お前.
  assert.equal(mark("お茶", [["茶", "new", null]], new Set([0])), "お茶");
  assert.equal(mark("お茶", [["茶", "new", null]]), "お茶");
  assert.equal(mark("お前", [["前", "new", null]], new Set([0])), "お前");
  assert.equal(mark("お前", [["前", "new", null]]), "お前");
  assert.equal(mark("お母さん", [["母", "new", null]]), "お母さん");
  assert.equal(mark("ご飯", [["飯", "new", null]]), "ご飯");
  assert.equal(mark("お茶", [["お茶", "new", null]], new Set([0])), "お茶(new,null)");
  assert.equal(mark("お茶とお菓子", [["お茶", "new", null], ["お菓子", "learned", null]]), "お茶(new,null) | と | お菓子(learned,null)");
  assert.equal(mark("ご家族は", [["家族", "new", "nakadaka"]], new Set([0, 1, 3])), "ご(new,null) | 家族(new,nakadaka) | は");
  assert.equal(mark("ご家族は", [["家族", "new", "nakadaka"]]), "ご(new,null) | 家族(new,nakadaka) | は");
  assert.equal(mark("お仕事", [["仕事", "new", null]]), "お(new,null) | 仕事(new,null)");
  // The word itself, when the deck holds it, wins over the prefix rule.
  assert.equal(mark("お風呂", [["お風呂", "learned", null], ["風呂", "new", null]], new Set([0, 1])), "お風呂(learned,null)");
  // Without a boundary after the prefix nothing is tried there.
  assert.equal(mark("お風呂", [["風呂", "new", null]], new Set([0])), "お風呂");
});

test("markWords leaves the particles after a word plain", () => {
  // A particle is not part of the word and has no card of its own: まで is plain after 領域,
  // で after 中, です after 学生. The word alone takes the colour.
  const book = [["本", "new", "heiban"]];
  assert.equal(mark("本には", book, new Set([0, 1, 2])), "本(new,heiban) | には");
  assert.equal(mark("本には", book), "本(new,heiban) | には");
  assert.equal(mark("本からは", book), "本(new,heiban) | からは");
  assert.equal(mark("本にはねよな", book), "本(new,heiban) | にはねよな");
  assert.equal(mark("学生です", [["学生", "learned", null]], new Set([0, 2])), "学生(learned,null) | です");
  assert.equal(mark("学生です", [["学生", "learned", null]]), "学生(learned,null) | です");
  assert.equal(mark("猫だよね", [["猫", "learned", null]]), "猫(learned,null) | だよね");
  assert.equal(mark("猫にほん", [["猫", "new", null]], new Set([0, 1, 2])), "猫(new,null) | にほん");
  assert.equal(mark("猫がでた", [["猫", "new", null]]), "猫(new,null) | がでた");
  assert.equal(mark("猫を見た", [["猫", "new", null], ["見る", "learned", null]]), "猫(new,null) | を | 見た(learned,null)");
  // A conjugation is part of the word and still goes with it; the particle after it does not.
  assert.equal(mark("食べるんだよね", [["食べる", "learned", null]]), "食べるんだ(learned,null) | よね");
  assert.equal(mark("食べたよ", [["食べる", "learned", null]], new Set([0])), "食べた(learned,null) | よ");
  // A deck word after the particle is found as ever, and a particle the deck itself holds as a
  // word is dropped by buildIndex, so it never colours anything.
  assert.equal(mark("犬はねこ", [["ねこ", "new", null]]), "犬は | ねこ(new,null)");
  assert.equal(mark("猫かもしれない", [["猫", "new", null], ["かもしれない", "learned", null]]), "猫(new,null) | かもしれない(learned,null)");
  assert.equal(mark("学生ですね", [["学生", "new", null], ["ですね", "learned", null]]), "学生(new,null) | ですね(learned,null)");
  // The runs still add up to the text, and the plain pieces are joined into one run.
  const runs = markWords("本にはねよな", buildIndex(book));
  assert.equal(runs.map((run) => run.text).join(""), "本にはねよな");
  assert.deepEqual(runs[1], { text: "にはねよな", status: null, pitch: null });
});

test("markWords colours the deck word alone in the lines the viewer read it wrong in", () => {
  // The two lines that showed the bug: 領域まで and 領域の read as one red piece, though まで and
  // の have no card. Exactly one run carries a card's status in each, and it is 領域; the note
  // names E4 and A4 are Latin text, blue by rule.
  const deck = [["領域", "new", null]];
  const lines = ["この時点でこっちの地声領域のE4に変えれる人なぁー", "で、余裕がある人はそのままA4の地声領域まで持っていってください。"];
  for (const line of lines) {
    const runs = markWords(line, buildIndex(deck));
    const coloured = runs.filter((run) => run.status && run.status !== "proper");
    assert.deepEqual(coloured, [{ text: "領域", status: "new", pitch: null }]);
    assert.equal(runs.map((run) => run.text).join(""), line);
  }
  assert.equal(mark(lines[0], deck), "この時点でこっちの地声 | 領域(new,null) | の | E4(proper,null) | に変えれる人なぁー");
  assert.equal(mark(lines[1], deck), "で、余裕がある人はそのまま | A4(proper,null) | の地声 | 領域(new,null) | まで持っていってください。");
});

test("markWords finds いう after という and っていう, not inside そういう", () => {
  const say = [["いう", "learned", null]];
  assert.equal(mark("という", say, new Set([0])), "と | いう(learned,null)");
  assert.equal(mark("という", say), "と | いう(learned,null)");
  assert.equal(mark("っていう", say, new Set([0])), "って | いう(learned,null)");
  assert.equal(mark("っていう", say), "って | いう(learned,null)");
  assert.equal(mark("彼という人", say, new Set([0, 1, 4])), "彼と | いう(learned,null) | 人");
  assert.equal(mark("彼という人", say), "彼と | いう(learned,null) | 人");
  assert.equal(mark("そういう", say, new Set([0])), "そういう");
  assert.equal(mark("そういう", say), "そういう");
  assert.equal(mark("そういうこと", say), "そういうこと");
  // The word must end the segment: いうな is one, and いう does not end it.
  assert.equal(mark("といった", say), "といった");
  assert.equal(mark("いう", say), "いう(learned,null)");
  // The quotative takes the colour of the run that ends where it begins: ICU holds it in one
  // segment with いう, so leaving it plain would cut that segment in two on screen.
  assert.equal(mark("猫という", [["猫", "new", null], ...say], new Set([0, 1])), "猫(new,null) | と(new,null) | いう(learned,null)");
  assert.equal(mark("猫という", [["猫", "new", null], ...say]), "猫(new,null) | と(new,null) | いう(learned,null)");
  assert.equal(mark("食べるという", [["食べる", "new", null], ...say]), "食べる(new,null) | と(new,null) | いう(learned,null)");
  assert.equal(mark("猫だっていう", [["猫", "new", null], ...say]), "猫(new,null) | だって | いう(learned,null)");
  // A deck word at the start wins: という itself, when a card holds it.
  assert.equal(mark("猫という", [["という", "learning", null], ...say]), "猫 | という(learning,null)");
  // The rule is for いう alone: ICU keeps ところ, とおる, とまる and とくに whole because they are
  // words of their own, and a card for ころ (頃), おる, まる or くに paints none of them.
  assert.equal(mark("ところ", [["ころ", "new", null]], new Set([0])), "ところ");
  assert.equal(mark("ところ", [["ころ", "new", null]]), "ところ");
  assert.equal(mark("今のところ", [["今", "new", null], ["ころ", "new", null]]), "今(new,null) | のところ");
  assert.equal(mark("私のところに来て", [["ころ", "new", null]]), "私のところに来て");
  assert.equal(mark("道をとおる", [["おる", "new", null]]), "道をとおる");
  assert.equal(mark("バスがとまる", [["まる", "new", null]]), "バスがとまる");
  assert.equal(mark("とくに", [["くに", "new", null]], new Set([0])), "とくに");
  assert.equal(mark("とくに", [["くに", "new", null]]), "とくに");
  // って before any other word is cut off by ICU, so that word begins a segment of its own.
  assert.equal(mark("ってこと", [["こと", "new", null]]), "って | こと(new,null)");
});

test("markWords finds a kana verb or adjective in its forms, at a word boundary only", () => {
  // ICU cuts し|まっ|た, わか|っ|た, つく|っ|た, おい|しか|っ|た and 話し|かけ|て|いただく.
  assert.equal(mark("しまった", [["しまう", "learned", null]], new Set([0, 1, 3])), "しまった(learned,null)");
  assert.equal(mark("しまった", [["しまう", "learned", null]]), "しまった(learned,null)");
  assert.equal(mark("わかった", [["わかる", "learned", null]], new Set([0, 2, 3])), "わかった(learned,null)");
  assert.equal(mark("わかった", [["わかる", "learned", null]]), "わかった(learned,null)");
  assert.equal(mark("わかりました", [["わかる", "learned", null]]), "わかりました(learned,null)");
  assert.equal(mark("つくった", [["つくる", "learned", null]]), "つくった(learned,null)");
  assert.equal(mark("つくれば", [["つくる", "learned", null]]), "つくれば(learned,null)");
  assert.equal(mark("もらった", [["もらう", "learned", null]]), "もらった(learned,null)");
  assert.equal(mark("おいしかった", [["おいしい", "learned", null]]), "おいしかった(learned,null)");
  assert.equal(mark("おいしくない", [["おいしい", "learned", null]]), "おいしくない(learned,null)");
  assert.equal(mark("すごかった", [["すごい", "learned", null]]), "すごかった(learned,null)");
  assert.equal(mark("すごく", [["すごい", "learned", null]]), "すごく(learned,null)");
  assert.equal(mark("びっくりした", [["びっくりする", "learned", null]]), "びっくりした(learned,null)");
  assert.equal(mark("びっくりだ", [["びっくりする", "learned", null]]), "びっくり(learned,null) | だ");
  assert.equal(mark("かけて", [["かける", "learned", null]], new Set([0, 2])), "かけて(learned,null)");
  assert.equal(mark("話しかけて", [["かける", "learned", null]], new Set([0, 1, 2, 4])), "話し | かけて(learned,null)");
  assert.equal(mark("話しかけて", [["かける", "learned", null]]), "話し | かけて(learned,null)");
  assert.equal(mark("食べてしまった", [["食べる", "new", null], ["しまう", "learned", null]]), "食べて(new,null) | しまった(learned,null)");
  assert.equal(mark("食べさせてもらった", [["食べる", "new", null], ["もらう", "learned", null]]), "食べさせて(new,null) | もらった(learned,null)");
  // The form must end at a boundary, as the exact word must: いれば is いる and ば (ば is no first
  // piece), and a kana stem alone is no noun form (かけ in かけ|ら, いれ in い|れ|ば).
  assert.equal(mark("いれば", [["いれる", "learned", null]], new Set([0, 1, 2])), "いれば");
  assert.equal(mark("いれば", [["いれる", "learned", null]]), "いれば");
  assert.equal(mark("かけら", [["かける", "learned", null]], new Set([0, 2])), "かけら");
  assert.equal(mark("かけら", [["かける", "learned", null]]), "かけら");
  assert.equal(mark("かけに行く", [["かける", "learned", null]]), "かけに行く");
  assert.equal(mark("しまいこむ", [["しまう", "learned", null]], new Set([0])), "しまいこむ");
  assert.equal(mark("しまい", [["しまう", "learned", null]], new Set([0])), "しまい(learned,null)");
  // The one-kana stems stay exact: いう is not in いった, する not in した.
  assert.equal(mark("いった", [["いう", "learned", null]]), "いった");
  assert.equal(mark("した", [["する", "learned", null]]), "した");
  assert.equal(mark("いい", [["いい", "learned", null]]), "いい(learned,null)");
  assert.equal(mark("いいよ", [["いい", "learned", null]]), "いい(learned,null) | よ");
  // A kanji verb's forms end where endsWord() lets them; a kana verb's at a boundary, or where
  // ICU fused the form's ending with the particle after it (below). A segment that begins at
  // the word is no such fusion.
  assert.equal(mark("食べたよ", [["食べる", "learned", null]], new Set([0])), "食べた(learned,null) | よ");
  assert.equal(mark("たべたよ", [["たべる", "learned", null]], new Set([0])), "たべたよ");
  assert.equal(mark("たべたよ", [["たべる", "learned", null]], new Set([0, 3])), "たべた(learned,null) | よ");
});

test("markWords ends a kana verb's form inside the segment ICU made of its ending and a particle", () => {
  // ICU fuses the sentence-final particle with た and ます (わか|っ|たよ, かけ|たよ, でき|ますよ,
  // ちがい|ますよ), so the form ends at no boundary: it may end where the rest of a segment that
  // began inside the form is a particle. The particle itself stays plain.
  const wakaru = [["わかる", "new", null]];
  assert.equal(mark("わかったよ", wakaru, new Set([0, 2, 3])), "わかった(new,null) | よ");
  assert.equal(mark("わかったよ", wakaru), "わかった(new,null) | よ");
  assert.equal(mark("わかったね", wakaru), "わかった(new,null) | ね");
  assert.equal(mark("わかったか", wakaru), "わかった(new,null) | か");
  assert.equal(mark("わかったの", wakaru), "わかった(new,null) | の");
  assert.equal(mark("すごかったね", [["すごい", "new", null]]), "すごかった(new,null) | ね");
  assert.equal(mark("おいしかったよ", [["おいしい", "new", null]]), "おいしかった(new,null) | よ");
  assert.equal(mark("できたよ", [["できる", "new", null]], new Set([0, 2])), "できた(new,null) | よ");
  assert.equal(mark("できますよ", [["できる", "new", null]]), "できます(new,null) | よ");
  assert.equal(mark("かけたよ", [["かける", "new", null]]), "かけた(new,null) | よ");
  assert.equal(mark("もらったよ", [["もらう", "new", null]]), "もらった(new,null) | よ");
  assert.equal(mark("ちがいますよ", [["ちがう", "new", null]]), "ちがいます(new,null) | よ");
  // The guards stand: the rest of the segment must be a particle, and the segment must begin
  // after the word (ことば, あいだ, はなし and こんにちは are one segment with こと, あい, はな and
  // こんにち; は|しか, as ICU cuts it, holds はし at no boundary).
  assert.equal(mark("いれば", [["いれる", "new", null]]), "いれば");
  assert.equal(mark("かけら", [["かける", "new", null]]), "かけら");
  assert.equal(mark("かけに行く", [["かける", "new", null]]), "かけに行く");
  assert.equal(mark("しまいこむ", [["しまう", "new", null]], new Set([0])), "しまいこむ");
  assert.equal(mark("あるいは", [["ある", "new", null]]), "あるいは");
  assert.equal(mark("ことば", [["こと", "new", null]], new Set([0])), "ことば");
  assert.equal(mark("ことば", [["こと", "new", null]]), "ことば");
  assert.equal(mark("あいだ", [["あい", "new", null]]), "あいだ");
  assert.equal(mark("はなし", [["はな", "new", null]]), "はなし");
  assert.equal(mark("こんにちは", [["こんにち", "new", null]]), "こんにちは");
  assert.equal(mark("はしか", [["はし", "new", null]], new Set([0, 1])), "はしか");
  // かけたらしい and かけたくさん (かけ|たらしい, かけ|たくさん) stay plain: らしい and くさん are no
  // particles, and NOT_BEFORE keeps た from ending the form before them.
  assert.equal(mark("かけたらしい", [["かける", "new", null]]), "かけたらしい");
  assert.equal(mark("かけたくさん", [["かける", "new", null]]), "かけたくさん");
});

test("markWords takes a kana word found whole over a form of it that adds particles alone", () => {
  // A kana noun ending in a verb's kana has a stem to the tables (いく + つ), so the copula after
  // it reads as a form's tail (いくつ + です): the word itself, at a boundary, wins over that, and
  // the copula is a particle of its own, with the status and without the overbar. ICU cuts
  // いくつ|です|か, けっこう|です, いくつ|で|しょう, いくつ|で|した.
  const some = [["いくつ", "new", "heiban"]];
  assert.equal(mark("いくつですか", some), "いくつ(new,heiban) | ですか");
  assert.equal(mark("いくつですか", some, new Set([0, 3, 5])), "いくつ(new,heiban) | ですか");
  assert.equal(mark("いくつですか", some, [0, 1, 2, 3, 4, 5]), "いくつ(new,heiban) | ですか");
  assert.equal(mark("いくつでしょう", some), "いくつ(new,heiban) | でしょう");
  assert.equal(mark("いくつでした", some), "いくつ(new,heiban) | でした");
  assert.equal(mark("けっこうです", [["けっこう", "new", "heiban"]]), "けっこう(new,heiban) | です");
  assert.equal(mark("きょうです", [["きょう", "new", "atamadaka"]]), "きょう(new,atamadaka) | です");
  assert.equal(mark("ふつうですね", [["ふつう", "learned", "heiban"]]), "ふつう(learned,heiban) | ですね");
  assert.equal(mark("ほんとうです", [["ほんとう", "new", "heiban"]]), "ほんとう(new,heiban) | です");
  // A kana verb or adjective loses nothing: its copula is plain either way, and a form that adds
  // more than particles still wins.
  assert.equal(mark("わかるんだ", [["わかる", "new", "heiban"]]), "わかる(new,heiban) | んだ");
  assert.equal(mark("おいしいです", [["おいしい", "new", "heiban"]]), "おいしい(new,heiban) | です");
  assert.equal(mark("わかるまい", [["わかる", "new", "heiban"]]), "わかるまい(new,heiban)");
  assert.equal(mark("わかりました", [["わかる", "new", "heiban"]]), "わかりました(new,heiban)");
  // Another word's form still overtakes the word found whole, and a kanji word keeps the form.
  assert.equal(mark("あるいて", [["ある", "new", null], ["あるく", "learned", null]]), "あるいて(learned,null)");
  assert.equal(mark("食べるでしょう", [["食べる", "new", "heiban"]]), "食べるでしょう(new,heiban)");
});

test("markWords finds くれる after the て ICU fused with its く", () => {
  // ICU cuts 食|べ|てく|れ|た, 書|い|てく|れ|た and かけ|てく|れ|た: the index after such a て is a
  // boundary to the matcher, where くれる begins and かけて ends.
  const deck = [["食べる", "new", null], ["くれる", "learned", null]];
  assert.equal(mark("食べてくれた", deck, new Set([0, 1, 2, 4, 5])), "食べて(new,null) | くれた(learned,null)");
  assert.equal(mark("食べてくれた", deck), "食べて(new,null) | くれた(learned,null)");
  assert.equal(mark("食べてくれない", deck), "食べて(new,null) | くれない(learned,null)");
  assert.equal(mark("食べてくれました", deck), "食べて(new,null) | くれました(learned,null)");
  assert.equal(mark("食べてくれる", deck), "食べて(new,null) | くれる(learned,null)");
  assert.equal(mark("書いてくれた", [["書く", "new", null], ["くれる", "learned", null]]), "書いて(new,null) | くれた(learned,null)");
  assert.equal(mark("かけてくれた", [["かける", "new", null], ["くれる", "learned", null]], new Set([0, 2, 4, 5])), "かけて(new,null) | くれた(learned,null)");
  assert.equal(mark("かけてくれた", [["かける", "new", null], ["くれる", "learned", null]]), "かけて(new,null) | くれた(learned,null)");
  assert.equal(mark("読んでくれた", [["読む", "new", null], ["くれる", "learned", null]]), "読んで(new,null) | くれた(learned,null)");
  // Only a て or で at a start, and only before く: nothing opens inside てき or after a て that
  // is not one.
  assert.equal(mark("てくれた", [["くれる", "learned", null]], new Set([0])), "て | くれた(learned,null)");
  assert.equal(mark("てきた", [["きる", "learned", null]], new Set([0])), "てきた");
  assert.equal(mark("すてくれた", [["くれる", "learned", null]], new Set([0])), "すてくれた");
  // The caller's set of starts is left as it was.
  const starts = new Set([0, 1, 2, 4, 5]);
  markWords("食べてくれた", buildIndex(deck), starts);
  assert.deepEqual([...starts], [0, 1, 2, 4, 5]);
});

test("markWords gives a kana word whose stem is a particle no forms", () => {
  // からい's stem から is a particle: からかった would be its かっ + た. The word itself is found.
  const hot = [["からい", "new", null]];
  assert.deepEqual(buildIndex(hot).stems.get("から"), undefined);
  assert.equal(mark("猫をからかった", hot), "猫をからかった");
  assert.equal(mark("からかった", hot), "からかった");
  assert.equal(mark("からい", hot), "からい(new,null)");
  assert.equal(mark("ならった", [["ならう", "new", null]]), "ならった");
  assert.equal(mark("しかった", [["しかる", "new", null]]), "しかった");
  // The kanji twin keeps its stem.
  assert.equal(mark("辛かった", [["辛い", "new", null]]), "辛かった(new,null)");
  assert.deepEqual(buildIndex([["辛い", "new", null]]).stems.get("辛").map((item) => item.kind), ["i-adj"]);
});

test("markWords stays quick on a long line against a large deck", () => {
  const entries = [];
  for (let i = 0; i < 3000; i++) entries.push([`語${i}る`, "learned", "heiban"]);
  entries.push(["日本語", "new", null]);
  const index = buildIndex(entries);
  const line = "これは日本語の字幕です。".repeat(400);
  const started = Date.now();
  const runs = markWords(line, index);
  assert.equal(runs.filter((run) => run.text === "日本語" && run.status === "new").length, 400);
  assert.equal(runs.filter((run) => run.status).length, 400);
  assert.equal(runs.map((run) => run.text).join(""), line);
  assert.ok(Date.now() - started < 1000, "marking a long line must not take a second");
});

// ------------------------------------------------------------------ markWords: what needs no card

// markWords with the options (`particles`, `katakana`, `names`, the last on unless a test says
// otherwise) and the known words.
function markWith(text, entries, opts, known, starts) {
  return shape(markWords(text, buildIndex(entries, known), starts, { ...NAMES_ON, ...opts }));
}

const PARTICLES_ON = { particles: true, names: true };
const BOTH_ON = { particles: true, katakana: true, names: true };

// The viewer's two lines (with the boundaries ICU gives them, Node 24) and a deck of the words they
// hold: 東京, 来る, よろしく, お願い, 今日, 前, こと, いう.
const MORNING = "おはようございます jr 東京駅に来てますよろしくお願いします";
const MORNING_STARTS = new Set([0, 4, 5, 6, 7, 9, 10, 12, 13, 15, 16, 17, 18, 19, 21, 25, 28, 29]);
const STATION = "さあ今日は東京駅丸の内駅舎の前からスタートということでですね";
const STATION_STARTS = new Set([0, 2, 4, 5, 7, 8, 11, 13, 14, 15, 17, 21, 24, 26, 27, 29]);
const VIEWER_DECK = ["東京", "来る", "よろしく", "お願い", "今日", "前", "こと", "いう"].map((word) => [word, "learned", null]);
// The viewer's own deck (13,730 notes) cut to the 14 entries that match anywhere in the two lines:
// the rest cannot change them, and they render the same with the whole deck. 丸 and 駅 are learned
// cards of their own, and 東京駅 and 丸の内, the longer names, still win.
const VIEWER_REAL_DECK = [
  ["今", "learned", "heiban"], ["来る", "learned", "heiban"], ["前", "learned", "heiban"], ["さあ", "learned", "heiban"], ["丸", "learned", null],
  ["今日", "learned", "atamadaka"], ["日", "learned", "heiban"], ["お願い", "learned", "heiban"], ["内", "learned", "heiban"], ["駅", "learned", "atamadaka"],
  ["こと", "learned", "atamadaka"], ["東京", "learned", "heiban"], ["東", "learned", null], ["駅舎", "learned", null],
];

test("the viewer's first line: jr and 東京駅 blue, the particle, the forms and お願いします learned", () => {
  // 東京 is in the deck, but 東京駅 (a place and its suffix) is the longer span from the same start
  // and takes it; ございます stays plain as a whole, since ます is the inflection of a verb the
  // deck lacks.
  const want =
    "おはようございます  | jr(proper,null) |   | 東京駅(proper,null) | に(learned,null) | 来てます(learned,null) | よろしく(learned,null) | お願いします(learned,null)";
  assert.equal(markWith(MORNING, VIEWER_DECK, PARTICLES_ON, [], MORNING_STARTS), want);
  assert.equal(markWith(MORNING, VIEWER_DECK, PARTICLES_ON), want);
  assert.equal(markWith(MORNING, VIEWER_DECK, BOTH_ON), want);
  assert.equal(markWith(MORNING, VIEWER_DECK.slice(1), PARTICLES_ON), want);
});

test("the viewer's second line: every particle learned, 東京駅 and 丸の内 blue, スタート with the option", () => {
  const off =
    "さあ | 今日(learned,null) | は(learned,null) | 東京駅(proper,null) | 丸の内(proper,null) | 駅舎 | の(learned,null) | 前(learned,null) | " +
    "から(learned,null) | スタート | と(learned,null) | いう(learned,null) | こと(learned,null) | で(learned,null) | です(learned,null) | ね(learned,null)";
  assert.equal(markWith(STATION, VIEWER_DECK, PARTICLES_ON, [], STATION_STARTS), off);
  assert.equal(markWith(STATION, VIEWER_DECK, PARTICLES_ON), off);
  assert.equal(markWith(STATION, VIEWER_DECK.filter(([word]) => word !== "東京"), PARTICLES_ON), off);
  assert.equal(markWith(STATION, VIEWER_DECK, BOTH_ON), off.replace("スタート |", "スタート(learned,null) |"));
  // 駅舎 as the deck says.
  assert.equal(markWith(STATION, [...VIEWER_DECK, ["駅舎", "new", null]], PARTICLES_ON), off.replace("駅舎 |", "駅舎(new,null) |"));
  // Without the option the particles stay plain, as 0.12.0 had them.
  assert.equal(
    markWith(STATION, VIEWER_DECK),
    "さあ | 今日(learned,null) | は | 東京駅(proper,null) | 丸の内(proper,null) | 駅舎の | 前(learned,null) | からスタートと | いう(learned,null) | こと(learned,null) | でですね",
  );
});

test("the viewer's lines with their own deck: 東京駅 and 丸の内 blue, and no の left white", () => {
  const station =
    "さあ(learned,heiban) | 今日(learned,atamadaka) | は(learned,null) | 東京駅(proper,null) | 丸の内(proper,null) | 駅舎(learned,null) | の(learned,null) | " +
    "前(learned,heiban) | から(learned,null) | スタート | という(learned,null) | こと(learned,atamadaka) | で(learned,null) | です(learned,null) | ね(learned,null)";
  assert.equal(markWith(STATION, VIEWER_REAL_DECK, PARTICLES_ON), station);
  assert.equal(markWith(STATION, VIEWER_REAL_DECK, BOTH_ON), station.replace("スタート |", "スタート(learned,null) |"));
  const runs = markWords(STATION, buildIndex(VIEWER_REAL_DECK), undefined, PARTICLES_ON);
  assert.ok(runs.filter((run) => run.text.includes("の")).every((run) => run.status));
  assert.equal(
    markWith(MORNING, VIEWER_REAL_DECK, PARTICLES_ON),
    "おはようございます  | jr(proper,null) |   | 東京駅(proper,null) | に(learned,null) | 来てます(learned,heiban) | よろしく | お願いします(learned,heiban)",
  );
});

test("markWords counts the particles as known with the option, wherever they stand", () => {
  // After a plain word, at the start of the line, after a deck word whatever its card says: a
  // particle is learned and never takes the colour of the word before it.
  assert.equal(markWith("猫が好き", [], PARTICLES_ON), "猫 | が(learned,null) | 好き");
  assert.equal(markWith("でも行く", [], PARTICLES_ON), "でも(learned,null) | 行く");
  assert.equal(markWith("猫が", [["猫", "new", null]], PARTICLES_ON), "猫(new,null) | が(learned,null)");
  assert.equal(markWith("領域まで", [["領域", "new", null]], PARTICLES_ON), "領域(new,null) | まで(learned,null)");
  assert.equal(markWith("猫が", [["猫", null, "heiban"]], PARTICLES_ON), "猫(null,heiban) | が(learned,null)");
  assert.equal(markWith("食べてから", [["食べる", "new", null]], PARTICLES_ON), "食べて(new,null) | から(learned,null)");
  // The longest entry at each start, one run each.
  assert.equal(markWith("本にはね", [["本", "new", "heiban"]], PARTICLES_ON), "本(new,heiban) | には(learned,null) | ね(learned,null)");
  assert.equal(markWith("学生ですね", [["学生", "new", null]], PARTICLES_ON), "学生(new,null) | です(learned,null) | ね(learned,null)");
  assert.equal(markWith("いいんじゃないかな", [], PARTICLES_ON), "いい(learned,null) | んじゃ(learned,null) | ない(learned,null) | かな(learned,null)");
  // Never over a deck word, a name or a katakana word, and never inside one.
  assert.equal(markWith("彼女のはなし", [["はなし", "new", null]], PARTICLES_ON), "彼女 | の(learned,null) | はなし(new,null)");
  assert.equal(markWith("jrが", [], PARTICLES_ON), "jr(proper,null) | が(learned,null)");
  assert.equal(markWith("コーヒーが", [], BOTH_ON), "コーヒー(learned,null) | が(learned,null)");
  assert.equal(markWith("行くかもしれない", [["行く", "new", null], ["かもしれない", "learning", null]], PARTICLES_ON), "行く(new,null) | かもしれない(learning,null)");
  // A particle ends at a word boundary: に is not in にほん.
  assert.equal(markWith("猫にほん", [["猫", "new", null]], PARTICLES_ON, [], new Set([0, 1])), "猫(new,null) | にほん");
  // Without the option they are plain text.
  assert.equal(markWith("猫が好き", [], undefined), "猫が好き");
  assert.equal(markWith("本にはね", [["本", "new", "heiban"]], { katakana: true }), "本(new,heiban) | にはね");
});

test("markWords counts the quotative with いう as a particle with the option", () => {
  const say = [["猫", "new", null], ["いう", "learning", null]];
  // A card for いう: the quotative before it is a particle, learned, not the colour of 猫.
  assert.equal(markWith("猫という", say, PARTICLES_ON), "猫(new,null) | と(learned,null) | いう(learning,null)");
  assert.equal(markWith("猫という", say), "猫(new,null) | と(new,null) | いう(learning,null)");
  // No card for いう: the segment ICU keeps whole is one particle combination.
  assert.equal(markWith("猫という", [["猫", "new", null]], PARTICLES_ON), "猫(new,null) | という(learned,null)");
  assert.equal(markWith("猫っていうのは", [["猫", "new", null]], PARTICLES_ON), "猫(new,null) | っていう(learned,null) | のは(learned,null)");
  assert.equal(markWith("ていうか", [], PARTICLES_ON), "ていう(learned,null) | か(learned,null)");
  assert.equal(markWith("猫という", [["猫", "new", null]]), "猫(new,null) | という");
  // A card for という itself wins, and does not reach into っていう; a card for いう never shows in
  // ていう, which no quotative fronts it with.
  assert.equal(markWith("猫という", [["猫", "new", null], ["という", "learning", null]], PARTICLES_ON), "猫(new,null) | という(learning,null)");
  assert.equal(markWith("ことっていうか", [["という", "learning", null]], PARTICLES_ON), "こと(learned,null) | っていう(learned,null) | か(learned,null)");
  assert.equal(markWith("ことていうか", [["いう", "learning", null]], PARTICLES_ON), "こと(learned,null) | ていう(learned,null) | か(learned,null)");
  // Without the option a quotative after a name stays plain: blue is no card's colour.
  assert.equal(markWith("jrという", [["いう", "learned", null]]), "jr(proper,null) | と | いう(learned,null)");
});

test("markWords refuses the particle shapes of a kana verb ICU cut up, as measured on the viewer's lines", () => {
  // The shapes that came up again and again among 2,198 particles on 668 real lines: the first kana
  // of やる, なる, よい, する, もらう, かかる, つながる, and the inflection of a verb the deck
  // lacks. The real segmenter cuts each line as the comment says.
  const cases = [
    ["ちょっと前からやってきました", "ちょっと(learned,null) | 前 | から(learned,null) | やって(learned,null) | きました(learned,null)"], // や|って|き|ました
    ["新幹線でやりましたけども", "新幹線 | で(learned,null) | やりました(learned,null) | けども(learned,null)"], // や|り|ました
    ["地獄コースになってるんで", "地獄コース | に(learned,null) | なってるんで(learned,null)"], // な|って|る
    ["ぜひよかったら", "ぜひよかったら"], // よ|か|っ|たら
    ["ご来場お待ちしております", "ご来場お待ち | して(learned,null) | おります(learned,null)"], // し|て|おり|ます: する and おる, grammar words
    ["反動してますね", "反動 | してます(learned,null) | ね(learned,null)"], // し|て|ます: する, a grammar word
    ["マス見てもらってね", "マス見 | て(learned,null) | もらって(learned,null) | ね(learned,null)"], // も|ら|って
    ["焼肉がかかってますからね", "焼肉 | が(learned,null) | かかってます | から(learned,null) | ね(learned,null)"], // か|かって
    ["運を使っちゃってる", "運 | を(learned,null) | 使っちゃってる"], // 使|っ|ちゃ|って|る
    ["おはようございます", "おはようございます"], // ご|ざ|い|ます
    ["いっぱいあったりとか", "いっぱいあったり | とか(learned,null)"], // あっ|たり|とか
  ];
  for (const [line, want] of cases) assert.equal(markWith(line, [], PARTICLES_ON), want, line);
  // 出た and もらった behind 猫, which ICU cuts 猫|が|で|た and 猫|に|も|ら|っ|た.
  const cat = [["猫", "new", null]];
  assert.equal(markWith("猫がでた", cat, PARTICLES_ON), "猫(new,null) | が(learned,null) | でた");
  assert.equal(markWith("猫がでた", cat, PARTICLES_ON, [], new Set([0, 1, 2, 3])), "猫(new,null) | が(learned,null) | でた");
  assert.equal(markWith("猫がでたよ", cat, PARTICLES_ON), "猫(new,null) | が(learned,null) | でたよ");
  assert.equal(markWith("猫にもらった", cat, PARTICLES_ON), "猫(new,null) | に(learned,null) | もらった(learned,null)");
  assert.equal(markWith("猫にもらった", cat, PARTICLES_ON, [], new Set([0, 1, 2, 3, 4, 5])), "猫(new,null) | に(learned,null) | もらった(learned,null)");
  // What it leaves: な before が (つ|な|が|っ|た), a shape too rare to list.
  assert.equal(markWith("名刺と つながったことで", [], PARTICLES_ON), "名刺 | と(learned,null) |  つ | な(learned,null) | がったこと | で(learned,null)");
  // What it costs: the Kansai copula や before った and って is やる's kana too.
  assert.equal(markWith("日本初やった", [], PARTICLES_ON), "日本(proper,null) | 初やった");
  assert.equal(markWith("休んだばっかやって", [["休む", "learned", null]], PARTICLES_ON), "休んだ(learned,null) | ばっか | やって(learned,null)");
});

test("markWords keeps the true particles the tempting guards would refuse", () => {
  // A particle before a kana word ICU cut into single kana (を|お|ご|ら, に|い|ます, で|ご|ざ|い):
  // "a particle before a single kana that is no particle" refused 25 such particles.
  assert.equal(markWith("高級焼肉をおごら", [], PARTICLES_ON), "高級焼肉 | を(learned,null) | おごら");
  assert.equal(markWith("会場にいますもんね", [], PARTICLES_ON), "会場 | に(learned,null) | います(learned,null) | もん(learned,null) | ね(learned,null)");
  assert.equal(markWith("優勝でございます", [], PARTICLES_ON), "優勝 | で(learned,null) | ございます");
  assert.equal(markWith("東京でたくさん", [], PARTICLES_ON), "東京(proper,null) | で(learned,null) | たくさん");
  // って after a particle or た, the nominaliser の and the copula だ before っ.
  assert.equal(markWith("首都高とかって", [], PARTICLES_ON), "首都高 | とか(learned,null) | って(learned,null)");
  assert.equal(markWith("逃したからって", [], PARTICLES_ON), "逃した | から(learned,null) | って(learned,null)");
  assert.equal(markWith("見るのって楽しい", [], PARTICLES_ON), "見る | の(learned,null) | って(learned,null) | 楽しい");
  assert.equal(markWith("こんな感じだったんです", [], PARTICLES_ON), "こんな(learned,null) | 感じ | だった(learned,null) | ん(learned,null) | です(learned,null)");
  // An inflection's shape after a particle or a kanji is the particle.
  assert.equal(markWith("お金がない", [], PARTICLES_ON), "お金 | が(learned,null) | ない(learned,null)");
});

test("markWords refuses the kana a kanji verb the deck lacks is cut into", () => {
  // ICU cuts 飲|ん|だ, 書|か|ない, 買|わ|なか|っ|た, 呼|ば|れ|た: the 音便 and the okurigana are the
  // verb's, whatever their shape.
  for (const verb of ["飲んだ", "呼んだ", "死んだ", "進んだ", "飲んだら", "書かない", "行かない", "話さない", "言わない", "遊ばない", "死なない", "買わなかった", "呼ばれた", "移される", "行かせて"]) {
    assert.equal(markWith(verb, [], PARTICLES_ON), verb, verb);
  }
  assert.equal(markWith("さっき休んだばっか", [], PARTICLES_ON), "さっき休んだばっか");
  // The particles beside them stay: が before ない, か after 何, ん after kana.
  assert.equal(markWith("本がない", [], PARTICLES_ON), "本 | が(learned,null) | ない(learned,null)");
  assert.equal(markWith("何かない", [], PARTICLES_ON), "何(learned,null) | か(learned,null) | ない(learned,null)");
  assert.equal(markWith("食べるんだ", [], PARTICLES_ON), "食べる | ん(learned,null) | だ(learned,null)");
});

test("markWords refuses the kana of slang and interjections ICU cut up, as measured on the viewer's lines", () => {
  const cases = [
    ["実質1万しか増えへんやんえ、なにこれ", "実質1万 | しか(learned,null) | 増えへんやんえ、 | なに(learned,null) | これ(learned,null)"], // な|に|これ
    ["おっしゃ!", "おっしゃ!"], // おっ|し|ゃ
    ["やばぁ!", "やばぁ!"], // や|ば|ぁ
    ["マスだせぇ", "マスだせぇ"], // だ|せ|ぇ
    ["申し訳なさそうに", "申し訳なさそう | に(learned,null)"], // な|さそう
    ["情けなさ", "情けなさ"], // な|さ
    ["でっけえやつね", "でっけえやつ | ね(learned,null)"], // で|っけ|え
    ["へえ", "へえ"], // へ|え
    ["かもしれない", "かも(learned,null) | しれない"], // かも|し|れ|ない
    ["それをしろ", "それ(learned,null) | を(learned,null) | しろ(learned,null)"], // し|ろ: する's, a grammar word
  ];
  for (const [line, want] of cases) assert.equal(markWith(line, [], PARTICLES_ON), want, line);
  const maybe = [["かもしれない", "learned", null], ["そう", "learned", null]];
  assert.equal(markWith("かもしれません", maybe, PARTICLES_ON), "かも(learned,null) | しれません");
  assert.equal(markWith("そうかもしれへん", maybe, PARTICLES_ON), "そう(learned,null) | かも(learned,null) | しれへん");
  // The true particles beside the same kana: な before におい or さかな, a particle drawn out.
  assert.equal(markWith("変なにおい", [], PARTICLES_ON), "変 | な(learned,null) | におい");
  assert.equal(markWith("好きなさかな", [], PARTICLES_ON), "好き | な(learned,null) | さかな");
  assert.equal(markWith("そうだよぉ", [], PARTICLES_ON), "そう(learned,null) | だ(learned,null) | よ(learned,null) | ぉ");
  assert.equal(markWith("そうかあ", [], PARTICLES_ON), "そう(learned,null) | か(learned,null) | あ");
});

test("markWords takes the particle ICU fused with the い of いる, and the one before a quotative", () => {
  // ICU cuts 人|がい|た and 猫|とい|た: no particle ends at a boundary there.
  assert.equal(markWith("人がいた", [], PARTICLES_ON), "人 | が(learned,null) | いた(learned,null)");
  assert.equal(markWith("人がいない", [], PARTICLES_ON), "人 | が(learned,null) | いない(learned,null)");
  assert.equal(markWith("猫がいれば", [], PARTICLES_ON), "猫 | が(learned,null) | いれば(learned,null)");
  assert.equal(markWith("猫といた", [], PARTICLES_ON), "猫 | と(learned,null) | いた(learned,null)");
  assert.equal(markWith("人がいる", [], PARTICLES_ON), "人 | が(learned,null) | いる(learned,null)");
  // Not at the end of the text, not は (靴|を|はい|た is 履いた), not the と of ておいて.
  assert.equal(markWith("猫がい", [], PARTICLES_ON), "猫がい");
  assert.equal(markWith("靴をはいた", [], PARTICLES_ON), "靴 | を(learned,null) | はいた");
  assert.equal(markWith("置いといて", [], PARTICLES_ON), "置いといて");
  // ね and よ before a quotative って of its own (楽しい|ね|って); な|って is なる's as often.
  assert.equal(markWith("楽しいねって", [], PARTICLES_ON), "楽しい | ね(learned,null) | って(learned,null)");
  assert.equal(markWith("すごいなって", [], PARTICLES_ON), "すごいなって");
  // って after a word the deck holds is the quotative, after ちゃ the verb's.
  assert.equal(markWith("さくらって", [["さくら", "learned", null]], PARTICLES_ON), "さくら(learned,null) | って(learned,null)");
  assert.equal(markWith("運を使っちゃってる", [["使う", "learned", null]], PARTICLES_ON), "運 | を(learned,null) | 使っちゃ(learned,null) | ってる");
});

// The boundaries of a line cut as "a|b|c": its text and the starts of its segments.
function cutLine(cut) {
  const starts = new Set([0]);
  let at = 0;
  for (const piece of cut.split("|")) starts.add((at += piece.length));
  const text = cut.replace(/\|/g, "");
  starts.delete(text.length);
  return [text, starts];
}

function markCut(cut, entries, opts = PARTICLES_ON) {
  const [text, starts] = cutLine(cut);
  return markWith(text, entries, opts, [], starts);
}

test("markWords finds the copula where Firefox's segmenter hides it", () => {
  // The viewer's line, as Firefox 156 cuts it: そうだ is one segment, so だ began none.
  const deck = ["上", "寝る", "結構", "良い", "十分", "広さ"].map((word) => [word, "learned", null]);
  assert.equal(
    markCut("上|で|寝る|の|も|結構|良さ|そうだ|な|。", deck),
    "上(learned,null) | で(learned,null) | 寝る(learned,null) | の(learned,null) | も(learned,null) | 結構(learned,null) | " +
      "良さ(learned,null) | そう(learned,null) | だ(learned,null) | な(learned,null) | 。",
  );
  // 広さ ends inside 広|さも: the も after it is a particle, ある a grammar word.
  assert.equal(
    markCut("十分|な|広|さも|ある|。", deck),
    "十分(learned,null) | な(learned,null) | 広さ(learned,null) | も(learned,null) | ある(learned,null) | 。",
  );
  assert.equal(markCut("大丈夫|そうだ", []), "大丈夫 | そう(learned,null) | だ(learned,null)");
  assert.equal(markCut("そうだ|ね", []), "そう(learned,null) | だ(learned,null) | ね(learned,null)");
  assert.equal(markCut("そうだね", []), "そう(learned,null) | だ(learned,null) | ね(learned,null)");
  // The copula cut in two, its second half fused with a particle or cut again (Node: だ|っ|たん).
  assert.equal(markCut("大変|だ|ったね", []), "大変 | だった(learned,null) | ね(learned,null)");
  assert.equal(markCut("学生|で|したね", []), "学生 | でした(learned,null) | ね(learned,null)");
  assert.equal(markCut("好き|だ|ろうね", []), "好き | だろう(learned,null) | ね(learned,null)");
  assert.equal(markCut("感じ|だ|っ|たん|です", []), "感じ | だった(learned,null) | ん(learned,null) | です(learned,null)");
  // An adjective's form runs on through the copula's past after そう.
  const fun = [["楽しい", "learned", null]];
  assert.equal(markCut("楽|しそう|だ|った", fun), "楽しそうだった(learned,null)");
  assert.equal(markCut("楽|し|そうだ|っ|た", fun), "楽しそうだった(learned,null)");
  // Only particles after the head: a word ICU knows is never cut (そうじ, ようやく), and without
  // the option nothing is drawn.
  assert.equal(markCut("そうじ|だ", []), "そうじ | だ(learned,null)");
  assert.equal(markCut("ようやく|だ", []), "ようやく | だ(learned,null)");
  assert.equal(markCut("大丈夫|そうだ", [], NAMES_ON), "大丈夫そうだ");
});

test("markWords counts the grammar words as known with the particles, and never inside another word", () => {
  // The verbs that carry the grammar, in the forms the lines have them, and the everyday words.
  assert.equal(markCut("本|が|あった", []), "本 | が(learned,null) | あった(learned,null)");
  assert.equal(markCut("ここ|に|いる", []), "ここ(learned,null) | に(learned,null) | いる(learned,null)");
  assert.equal(markCut("見|て|み|た", []), "見 | て(learned,null) | みた(learned,null)");
  assert.equal(markCut("持|って|きた", []), "持 | って(learned,null) | きた(learned,null)");
  assert.equal(markCut("来|て|おり|ます", []), "来 | て(learned,null) | おります(learned,null)");
  assert.equal(markCut("勉強|する", []), "勉強 | する(learned,null)");
  assert.equal(markCut("まだ|だ", []), "まだ(learned,null) | だ(learned,null)");
  assert.equal(markCut("大人|らしい", []), "大人 | らしい(learned,null)");
  assert.equal(markCut("どうし|よう", []), "どうしよう(learned,null)");
  // A card says more than the list.
  assert.equal(markCut("本|が|ある", [["ある", "new", null]]), "本 | が(learned,null) | ある(new,null)");
  // Never inside a word ICU keeps whole, nor on the okurigana of a word it cut up.
  for (const cut of ["いただきます", "ありがとう", "したがって", "あるいは", "いくら", "書|い|た", "ご|ざ|い|ます"]) {
    assert.equal(markCut(cut, []), cut.replace(/\|/g, ""), cut);
  }
  // なって after い or だ is the sentence-final な and a quotative.
  assert.equal(markWith("すごいなって", [], PARTICLES_ON), "すごいなって");
  // Without the option they stay plain.
  assert.equal(markCut("本|が|あった", [], NAMES_ON), "本があった");
});

test("markWords runs a noun on over する's forms", () => {
  assert.equal(markWith("勉強している", [["勉強", "new", "heiban"]]), "勉強している(new,heiban)");
  assert.equal(markWith("お願いします", [["お願い", "learning", null]]), "お願いします(learning,null)");
  assert.equal(markWith("勉強させられた", [["勉強", "new", null]]), "勉強させられた(new,null)");
  assert.equal(markWith("スタートしました", [], undefined, ["スタート"]), "スタートしました(learned,null)");
  // The bare noun stays a match, and a lone し is no form (the conjunctive particle as often).
  assert.equal(markWith("勉強です", [["勉強", "new", null]]), "勉強(new,null) | です");
  assert.equal(markWith("勉強し、", [["勉強", "new", null]]), "勉強(new,null) | し、");
  assert.equal(markWith("勉強しか", [["勉強", "new", null]], PARTICLES_ON), "勉強(new,null) | しか(learned,null)");
  // Not after a name, nor for a kana word, nor for a word that ends in する (its own forms).
  assert.equal(markWith("東京駅する", [["東京", "learned", null]]), "東京駅(proper,null) | する");
  assert.equal(markWith("びっくりした", [["びっくり", "new", null]]), "びっくり(new,null) | した");
  assert.equal(markWith("勉強している", [["勉強する", "learned", null]]), "勉強している(learned,null)");
});

test("markWords gives a card for the verb its own forms over a noun's する", () => {
  // The viewer's deck holds 話 and 話す, 回 and 回す, 足 and 足す: the form of the verb is as long
  // as the noun with する, and the verb's card has it.
  const talk = [["話", "new", "heiban"], ["話す", "learned", "nakadaka"]];
  assert.equal(markWith("話して", talk), "話して(learned,nakadaka)");
  assert.equal(markWith("話します", talk), "話します(learned,nakadaka)");
  assert.equal(markWith("マス回しますね", [["回", "learned", "atamadaka"], ["回す", "learned", "heiban"]]), "マス | 回します(learned,heiban) | ね");
  assert.equal(markWith("思い出して", [["思い出", "new", "heiban"], ["思い出す", "learned", "nakadaka"]]), "思い出して(learned,nakadaka)");
  // Without a card for the verb the noun runs on as before.
  assert.equal(markWith("思い出して", [["思い出", "new", "heiban"]]), "思い出して(new,heiban)");
});

test("markWords runs no time word, adverb, pronoun, counter or single kanji on over する", () => {
  // Real lines with the viewer's cards for 何, 顔, 数 and 台.
  assert.equal(markWith("何してるんですか", [["何", "suspended", "heiban"]]), "何(suspended,heiban) | してるんですか");
  assert.equal(markWith("腹立つ顔するやつ", [["顔", "learned", "heiban"]]), "腹立つ | 顔(learned,heiban) | するやつ");
  assert.equal(markWith("数するなんすか", [["数", "learned", "atamadaka"]]), "数(learned,atamadaka) | するなんすか");
  assert.equal(markWith("4台しちゃった", [["台", "learned", null]]), "4 | 台(learned,null) | しちゃった");
  for (const word of ["何か", "何も", "少し", "全然", "絶対", "一番", "結構", "多分", "毎日", "今日", "後で", "一回"]) {
    for (const verb of ["したい", "しない", "します", "してる"]) {
      assert.equal(markWith(word + verb, [[word, "learned", null]]), `${word}(learned,null) | ${verb}`, word + verb);
    }
  }
  // The する nouns still do, a noun ending in し among them.
  assert.equal(markWith("説明します", [["説明", "new", null]]), "説明します(new,null)");
  assert.equal(markWith("引っ越ししました", [["引っ越し", "new", null]]), "引っ越ししました(new,null)");
  assert.equal(markWith("お願いします", [["お願い", "new", null]], PARTICLES_ON), "お願いします(new,null)");
});

test("markWords never ends a noun's する run inside ちゃった", () => {
  // ICU cuts 勉強|し|ちゃ|っ|た: the tables cannot follow ちゃった, and a run ending at ちゃ kept a
  // card for ちゃう from the rest of it.
  const tired = [["ちゃう", "learned", "heiban"]];
  assert.equal(markWith("勉強しちゃった", [["勉強", "new", null], ...tired]), "勉強(new,null) | し | ちゃった(learned,heiban)");
  assert.equal(markWith("渋滞しちゃってる", [["渋滞", "learned", null], ...tired]), "渋滞(learned,null) | し | ちゃってる(learned,heiban)");
  assert.equal(markWith("勉強しちゃった", [["勉強", "new", null]]), "勉強(new,null) | しちゃった");
  // ちゃう itself is a form the tables follow.
  assert.equal(markWith("勉強しちゃう", [["勉強", "new", null], ...tired]), "勉強しちゃう(new,null)");
});

test("markWords colours Latin text as a name", () => {
  assert.equal(markWith("jr", []), "jr(proper,null)");
  assert.equal(markWith("abc", []), "abc(proper,null)");
  assert.equal(markWith("JRで", [], PARTICLES_ON), "JR(proper,null) | で(learned,null)");
  assert.equal(markWith("YouTubeを見る", [["見る", "learned", null]]), "YouTube(proper,null) | を | 見る(learned,null)");
  assert.equal(markWith("iPhoneとTV", []), "iPhone(proper,null) | と | TV(proper,null)");
  assert.equal(markWith("rock'n'roll", []), "rock'n'roll(proper,null)");
  assert.equal(markWith("ＪＲ東日本", []), "ＪＲ(proper,null) | 東日本");
  assert.equal(markWith("E4に", []), "E4(proper,null) | に");
  // Digits alone are a number, and a letter inside a segment starts nothing.
  assert.equal(markWith("123と456", []), "123と456");
  assert.equal(markWith("3D", []), "3D");
  // A card for the word wins the tie; a longer one wins outright.
  assert.equal(markWith("OKです", [["OK", "new", null]]), "OK(new,null) | です");
  assert.equal(markWith("Tシャツ", [["Tシャツ", "new", null]]), "Tシャツ(new,null)");
});

test("markWords keeps full-width Latin whole, a letter with its katakana, and laughter plain", () => {
  // ICU cuts Ｗｉ|－|Ｆｉ and Ｑ|＆|Ａ: the full-width run takes the marks the half-width one does.
  assert.equal(markWith("Ｗｉ－Ｆｉ", []), "Ｗｉ－Ｆｉ(proper,null)");
  assert.equal(markWith("Ｑ＆Ａです", []), "Ｑ＆Ａ(proper,null) | です");
  assert.equal(markWith("Wi-Fi", []), "Wi-Fi(proper,null)");
  // T|シャツ and J|リーグ are one word each.
  assert.equal(markWith("Tシャツを着る", []), "Tシャツ(proper,null) | を着る");
  assert.equal(markWith("Ｔシャツ", [], { katakana: true }), "Ｔシャツ(proper,null)");
  assert.equal(markWith("Jリーグ", [], undefined, [], new Set([0, 1])), "Jリーグ(proper,null)");
  // Laughter is no name.
  for (const laugh of ["www", "ｗｗｗ", "wwwww", "草www"]) assert.equal(markWith(laugh, []), laugh);
});

test("markWords colours a place name, and a place with its suffix, as a name", () => {
  for (const place of ["東京都", "北海道", "丸の内", "アメリカ", "大阪城", "渋谷区"]) assert.equal(markWith(place, []), `${place}(proper,null)`);
  // A suffix segment after a place (品川|駅) or ending the segment the place began (東京駅 whole).
  assert.equal(markWith("品川駅", []), "品川駅(proper,null)");
  assert.equal(markWith("東京駅", [], undefined, [], new Set([0])), "東京駅(proper,null)");
  assert.equal(markWith("東京駅", [], undefined, [], new Set([0, 2])), "東京駅(proper,null)");
  // A kanji segment of two or more with a suffix segment after it; ICU cuts 東|急|線, which takes
  // none.
  assert.equal(markWith("東急線", [], undefined, [], new Set([0, 2])), "東急線(proper,null)");
  assert.equal(markWith("東急線", []), "東急線");
  // What is no suffix: 駅舎 and 駅前 are words, and a suffix after a particle is plain.
  assert.equal(markWith("丸の内駅舎", []), "丸の内(proper,null) | 駅舎");
  assert.equal(markWith("東京駅前", []), "東京(proper,null) | 駅前");
  assert.equal(markWith("東京の駅", [], PARTICLES_ON), "東京(proper,null) | の(learned,null) | 駅");
  // A place ends a word: 日本 is not in 日本語 or 日本人.
  assert.equal(markWith("日本語", []), "日本語");
  assert.equal(markWith("日本人", []), "日本人");
  assert.equal(markWith("日本の", []), "日本(proper,null) | の");
});

test("markWords gives the longer span to a name or a card, and the tie to the card", () => {
  // The viewer's deck holds 東京, 駅 and 丸: 東京駅 and 丸の内 are longer from the same start.
  assert.equal(markWith("東京駅に行く", [["東京", "learned", null], ["駅", "learned", null]]), "東京駅(proper,null) | に行く");
  assert.equal(markWith("丸の内", [["丸", "learned", null]]), "丸の内(proper,null)");
  // 東京 alone is a tie, and the card has it; a longer card has its word.
  assert.equal(markWith("東京に行く", [["東京", "learned", null]]), "東京(learned,null) | に行く");
  assert.equal(markWith("東京駅", [["東京駅", "new", null]]), "東京駅(new,null)");
  assert.equal(markWith("日本語", [["日本語", "new", null]]), "日本語(new,null)");
  // A known word likewise: the same length wins, a shorter one does not.
  assert.equal(markWith("東京駅", [], undefined, ["東京駅"]), "東京駅(learned,null)");
  assert.equal(markWith("東京駅", [["東京", "new", null]], undefined, ["東京"]), "東京駅(proper,null)");
});

test("markWords joins a suffix only to a segment no card begins at", () => {
  // The viewer's own cards: a word and the noun after it (結構|山, 昨日|海, 地元|駅, 天然|温泉, as
  // ICU cuts them) are two words, each in its card's colour.
  const real = [
    ["結構", "learned", "heiban"], ["山", "learned", "nakadaka"], ["昨日", "learned", "heiban"], ["海", "learned", null], ["地元", "learned", "heiban"],
    ["駅", "learned", "atamadaka"], ["天然", "learned", null], ["温泉", "suspended", "heiban"], ["東京", "learned", "heiban"], ["丸", "learned", null],
    ["駅舎", "learned", null],
  ];
  assert.equal(markWith("これ結構山かったよね", real), "これ | 結構(learned,heiban) | 山(learned,nakadaka) | かったよね");
  assert.equal(markWith("昨日海に行った", real), "昨日(learned,heiban) | 海(learned,null) | に行った");
  assert.equal(markWith("地元駅で降りる", real), "地元(learned,heiban) | 駅(learned,atamadaka) | で降りる");
  assert.equal(markWith("天然温泉", real), "天然(learned,null) | 温泉(suspended,heiban)");
  // No card for 主要: the pair is a name, as before; and a place with its suffix still wins.
  assert.equal(markWith("主要駅", real), "主要駅(proper,null)");
  assert.equal(markWith("東京駅丸の内駅舎", real, PARTICLES_ON), "東京駅(proper,null) | 丸の内(proper,null) | 駅舎(learned,null)");
});

// The name switch, off by default: no card stands behind a name or Latin text, so the matcher
// still reads it whole (no deck word begins inside it) but draws it as plain text.
const NAMES_OFF = Object.freeze({ names: false });

test("markWords without the names option keeps a name whole and plain, save a katakana head the katakana option takes", () => {
  assert.equal(shape(markWords("OKよ。", buildIndex([]))), "OKよ。");
  assert.equal(markWith("OKよ。", [], NAMES_OFF), "OKよ。");
  assert.ok(markWords("iPhoneとTV", buildIndex([])).every((run) => run.status === null && run.pitch === null));
  // No deck word is found inside a Latin run, even at a boundary the caller gives.
  assert.equal(markWith("OKよ", [["K", "new", "heiban"]], NAMES_OFF, [], new Set([0, 1, 2])), "OKよ");
  assert.equal(markWith("OKよ", [["K", "new", "heiban"]], undefined, [], new Set([0, 1, 2])), "OK(proper,null) | よ");
  // Nor inside a place, which still outruns a shorter card from its start.
  const deck = [["東京", "learned", "heiban"], ["駅", "learned", "atamadaka"], ["丸", "learned", null], ["行く", "new", null]];
  assert.equal(markWith("東京駅に行く", deck, NAMES_OFF, [], new Set([0, 2, 3, 4])), "東京駅に | 行く(new,null)");
  assert.equal(markWith("丸の内", deck, NAMES_OFF), "丸の内");
  // A card the same length as the name keeps its colour, as with the option.
  assert.equal(markWith("東京に行く", deck, NAMES_OFF), "東京(learned,heiban) | に | 行く(new,null)");
  // A place written in kanji stays one plain piece: a card for 京 is not found inside 東京.
  assert.equal(markWith("東京に", [["京", "new", null]], NAMES_OFF, [], new Set([0, 1, 2])), "東京に");
  // The katakana switch keeps what it asked for: a name's katakana head is learned, the rest of
  // the name plain and still closed to the deck (駅 in スカイツリー駅). Without it, plain.
  assert.equal(markWith("アメリカで", [], { katakana: true, names: false }), "アメリカ(learned,null) | で");
  assert.equal(markWith("アメリカで", [], NAMES_OFF), "アメリカで");
  assert.equal(markWith("スカイツリー駅", [["駅", "new", null]], { katakana: true, names: false }, [], new Set([0, 6])), "スカイツリー(learned,null) | 駅");
  // The particle rule never takes a name over, and a Latin head is no katakana run.
  assert.equal(markWith("アメリカで", [], { particles: true, names: false }), "アメリカ | で(learned,null)");
  assert.equal(markWith("Tシャツ", [], { katakana: true, names: false }), "Tシャツ");
  // A quotative after a name takes no colour from it, as before.
  assert.equal(markWith("JRっていう", [["いう", "new", null]], NAMES_OFF), "JRって | いう(new,null)");
});

test("markWords leaves the chili of a dish a word, not Chile", () => {
  assert.equal(markWith("エビチリ", []), "エビチリ");
  assert.equal(markWith("チリソース", [], { katakana: true }), "チリソース(learned,null)");
  assert.equal(markWith("チリソース", []), "チリソース");
});

test("markWords counts the katakana words as known with the option", () => {
  assert.equal(markWith("スタート", [], { katakana: true }), "スタート(learned,null)");
  assert.equal(markWith("スタート", []), "スタート");
  assert.equal(markWith("ジョン・スミス", [], { katakana: true }), "ジョン・スミス(learned,null)");
  assert.equal(markWith("ラーメン屋", [], { katakana: true }), "ラーメン(learned,null) | 屋");
  // A single katakana character is no word.
  assert.equal(markWith("アが", [], BOTH_ON), "ア | が(learned,null)");
  // A card keeps its status, a place stays blue, and a deck word inside the run takes its own.
  assert.equal(markWith("コーヒー", [["コーヒー", "new", "heiban"]], { katakana: true }), "コーヒー(new,heiban)");
  assert.equal(markWith("アメリカ", [], { katakana: true }), "アメリカ(proper,null)");
  assert.equal(markWith("コーヒーカップ", [["カップ", "new", null]], { katakana: true }, [], new Set([0, 4])), "コーヒー(learned,null) | カップ(new,null)");
});

test("markWords colours a known word as learned, over its card and in its forms", () => {
  assert.equal(markWith("猫", [["猫", "new", "atamadaka"]], undefined, ["猫"]), "猫(learned,atamadaka)");
  assert.equal(markWith("走った", [], undefined, ["走る"]), "走った(learned,null)");
  assert.equal(markWith("はしった", [], undefined, ["はしる"]), "はしった(learned,null)");
  // A kana reading's entry (a card usually written in kana) is overridden like any entry.
  assert.equal(markWith("さらに言う", [["更に", "new", null], ["さらに", "new", null]], undefined, ["さらに"]), "さらに(learned,null) | 言う");
});

test("markWords stays quick with every option on", () => {
  const entries = [];
  for (let i = 0; i < 3000; i++) entries.push([`語${i}る`, "learned", "heiban"]);
  entries.push(["日本語", "new", null]);
  const index = buildIndex(entries, ["字幕"]);
  const line = "これは日本語の字幕ですよね、jrで東京駅からスタートということで。".repeat(150);
  const started = Date.now();
  const runs = markWords(line, index, undefined, BOTH_ON);
  assert.equal(runs.filter((run) => run.text === "日本語" && run.status === "new").length, 150);
  assert.equal(runs.filter((run) => run.text === "東京駅" && run.status === "proper").length, 150);
  assert.equal(runs.map((run) => run.text).join(""), line);
  assert.ok(Date.now() - started < 1000, "marking a long line must not take a second");
});
