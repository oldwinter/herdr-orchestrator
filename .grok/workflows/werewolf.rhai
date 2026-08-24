let meta = #{
    name: "werewolf",
    description: "Deal 10 Grok villagers fixed hidden roles, then play one night and one day of werewolf",
    when_to_use: "When you want ten parallel Grok players to act a short werewolf game",
    phases: [
        #{ title: "Night", detail: "10 villagers take private night actions" },
        #{ title: "Witch", detail: "witch sees the intended kill and chooses save or poison" },
        #{ title: "Day", detail: "living villagers speak, then vote" },
        #{ title: "Chronicle", detail: "one narrator writes the public tale" },
    ],
};

let night_schema = #{
    "type": "object",
    "required": ["action", "target", "private_note", "public_dream"],
    "properties": #{
        "action": #{ "type": "string" },
        "target": #{ "type": "string" },
        "private_note": #{ "type": "string" },
        "public_dream": #{ "type": "string" },
    },
};
let witch_schema = #{
    "type": "object",
    "required": ["action", "target", "private_note"],
    "properties": #{
        "action": #{ "type": "string" },
        "target": #{ "type": "string" },
        "private_note": #{ "type": "string" },
    },
};
let speech_schema = #{
    "type": "object",
    "required": ["speech", "suspect"],
    "properties": #{
        "speech": #{ "type": "string" },
        "suspect": #{ "type": "string" },
    },
};
let vote_schema = #{
    "type": "object",
    "required": ["vote_for", "reason"],
    "properties": #{
        "vote_for": #{ "type": "string" },
        "reason": #{ "type": "string" },
    },
};
let chronicle_schema = #{
    "type": "object",
    "required": ["title", "body"],
    "properties": #{
        "title": #{ "type": "string" },
        "body": #{ "type": "string" },
    },
};

fn trimmed(s) {
    if type_of(s) == "string" {
        s.trim();
        s
    } else {
        ""
    }
}

fn living_named(players, name) {
    let want = trimmed(name);
    if want == "" { return ""; }
    for p in players {
        if p.alive && p.name == want { return p.name; }
    }
    ""
}

fn role_of(players, name) {
    for p in players {
        if p.name == name { return p.role_label; }
    }
    "未知"
}

fn camp_of(players, name) {
    for p in players {
        if p.name == name { return p.camp; }
    }
    "good"
}

fn roster_text(players) {
    let text = "";
    for p in players {
        let mark = if p.alive { "在世" } else { "已出局" };
        text += "- " + p.name + "（" + p.job + "，" + mark + "）\n";
    }
    text
}

fn wolf_names(players) {
    let text = "";
    for p in players {
        if p.role == "wolf" {
            if text != "" { text += "、"; }
            text += p.name;
        }
    }
    text
}

fn count_alive_camp(players, camp) {
    let n = 0;
    for p in players {
        if p.alive && p.camp == camp { n += 1; }
    }
    n
}

fn winner_of(players) {
    let wolves = count_alive_camp(players, "wolf");
    let good = count_alive_camp(players, "good");
    if wolves == 0 { return "村民阵营"; }
    if wolves >= good { return "狼人阵营"; }
    "尚未分出胜负"
}

let village = "青石镇";
if args != () && args.village != () {
    let custom = trimmed(args.village);
    if custom != "" { village = custom; }
}

// Fixed deal — workflows cannot roll dice. Ten seats, classic 10-player mix.
let players = [
    #{ name: "村长老张", job: "村长", role: "villager", role_label: "村民", camp: "good", alive: true },
    #{ name: "铁匠阿强", job: "铁匠", role: "wolf", role_label: "狼人", camp: "wolf", alive: true },
    #{ name: "医生林晚", job: "大夫", role: "seer", role_label: "预言家", camp: "good", alive: true },
    #{ name: "教师苏晴", job: "教师", role: "villager", role_label: "村民", camp: "good", alive: true },
    #{ name: "猎户周山", job: "猎户", role: "hunter", role_label: "猎人", camp: "good", alive: true },
    #{ name: "寡妇陈姨", job: "药铺老板", role: "witch", role_label: "女巫", camp: "good", alive: true },
    #{ name: "少年小满", job: "学徒", role: "wolf", role_label: "狼人", camp: "wolf", alive: true },
    #{ name: "旅人北风", job: "外乡人", role: "villager", role_label: "村民", camp: "good", alive: true },
    #{ name: "厨师老白", job: "厨子", role: "guard", role_label: "守卫", camp: "good", alive: true },
    #{ name: "守夜人灰", job: "更夫", role: "villager", role_label: "村民", camp: "good", alive: true },
];

let names_line = "";
for p in players {
    if names_line != "" { names_line += "、"; }
    names_line += p.name;
}

log("青石镇开局：10 个 Grok 村民入座，身份由主持人暗发，不掷骰。");

phase("Night");
let night_jobs = [];
for p in players {
    let prompt = "";
    prompt += "你正在玩一局中文狼人杀角色扮演。不要使用任何工具，不要读仓库，不要跳出角色。\n";
    prompt += "村庄：" + village + "。今晚是第一夜。在场十人：\n" + roster_text(players);
    prompt += "你的公开身份是「" + p.name + "，" + p.job + "」。\n";
    prompt += "你的隐藏身份是「" + p.role_label + "」。绝对不要在 public_dream 里泄漏隐藏身份。\n";
    prompt += "合法目标名单（必须精确使用其中一个全名，或不选则填空字符串）：" + names_line + "\n";
    if p.role == "wolf" {
        prompt += "你的狼队友是：" + wolf_names(players) + "。你们不能刀自己人。\n";
        prompt += "action 只能填 kill。target 填一个好人的全名。private_note 写给队友看的刀口理由。public_dream 写一段普通村民的梦话。\n";
    } else if p.role == "seer" {
        prompt += "action 只能填 inspect。target 填你要查验的全名。private_note 写你为什么查这个人。public_dream 写普通梦话。\n";
    } else if p.role == "guard" {
        prompt += "action 只能填 protect。target 填你要守护的全名，可以是自己。private_note 写守护理由。public_dream 写普通梦话。\n";
    } else if p.role == "witch" {
        prompt += "第一夜你先不行动。action 填 wait，target 填空字符串。private_note 写你会如何使用药。public_dream 写普通梦话。\n";
    } else {
        prompt += "你没有夜间技能。action 填 sleep，target 填空字符串。private_note 写你对谁起疑。public_dream 写一段不超过 40 字的梦话。\n";
    }
    prompt += "只输出符合 schema 的字段，中文，短句。";
    night_jobs.push(#{
        prompt: prompt,
        label: "night:" + p.name,
        capability_mode: "read-only",
        model: "grok-4.6",
        output_schema: night_schema,
    });
}
let night_results = parallel(night_jobs);

let wolf_targets = [];
let seer_target = "";
let guard_target = "";
let dreams = [];
let i = 0;
for p in players {
    let r = night_results[i];
    i += 1;
    if r == () || !r.success || r.output == () { continue; }
    let action = trimmed(r.output.action);
    let target = living_named(players, r.output.target);
    let dream = trimmed(r.output.public_dream);
    if dream != "" { dreams.push(p.name + "梦见：" + dream); }
    if p.role == "wolf" && action == "kill" && target != "" && camp_of(players, target) != "wolf" {
        wolf_targets.push(target);
    }
    if p.role == "seer" && action == "inspect" { seer_target = target; }
    if p.role == "guard" && action == "protect" { guard_target = target; }
}

let intended_kill = "";
if wolf_targets.len() > 0 {
    intended_kill = wolf_targets[0];
    for cand in wolf_targets {
        if cand == wolf_targets[0] { intended_kill = cand; }
    }
    // Prefer a name both wolves said. Otherwise keep the first valid blade.
    let agreed = "";
    for cand in wolf_targets {
        let n = 0;
        for other in wolf_targets {
            if other == cand { n += 1; }
        }
        if n >= 2 { agreed = cand; }
    }
    if agreed != "" { intended_kill = agreed; }
}

let seer_result = "";
if seer_target != "" {
    let seen_camp = camp_of(players, seer_target);
    seer_result = if seen_camp == "wolf" { "狼人" } else { "好人" };
}

log("第一夜刀口意向：" + (if intended_kill == "" { "无人" } else { intended_kill }) + "；守卫守在：" + (if guard_target == "" { "无人" } else { guard_target }));

phase("Witch");
let witch_action = "none";
let witch_poison = "";
let witch_player = ();
for p in players {
    if p.role == "witch" && p.alive { witch_player = p; }
}
if witch_player != () {
    let wprompt = "";
    wprompt += "你正在玩中文狼人杀。不要使用工具，不要跳出角色。\n";
    wprompt += "你是「" + witch_player.name + "」，隐藏身份是女巫。在场十人：\n" + roster_text(players);
    wprompt += "今晚狼人准备刀的是：「" + (if intended_kill == "" { "无人" } else { intended_kill }) + "」。\n";
    wprompt += "你有一瓶解药和一瓶毒药，各只能用一次。第一夜可以用解药。\n";
    wprompt += "action 只能是 save、poison 或 none。\n";
    wprompt += "save：救下今晚的刀口，target 填空字符串。\n";
    wprompt += "poison：毒一个活人，target 必须是精确全名，且不能是你自己。\n";
    wprompt += "none：不用药，target 填空字符串。\n";
    wprompt += "private_note 写你的判断。只输出 schema 字段。";
    let wr = agent(wprompt, #{
        label: "witch:" + witch_player.name,
        capability_mode: "read-only",
        model: "grok-4.6",
        output_schema: witch_schema,
    });
    if wr != () && wr.success && wr.output != () {
        witch_action = trimmed(wr.output.action);
        witch_poison = living_named(players, wr.output.target);
        if witch_poison == witch_player.name { witch_poison = ""; }
    }
}

let night_dead = [];
let wolf_blocked = intended_kill != "" && intended_kill == guard_target;
let saved = witch_action == "save" && intended_kill != "";
if intended_kill != "" && !wolf_blocked && !saved {
    night_dead.push(intended_kill);
}
if witch_action == "poison" && witch_poison != "" {
    let already = false;
    for n in night_dead {
        if n == witch_poison { already = true; }
    }
    if !already { night_dead.push(witch_poison); }
}
let di = 0;
for p in players {
    for n in night_dead {
        if p.name == n { players[di].alive = false; }
    }
    di += 1;
}

let night_dead_text = "平安夜，无人出局";
if night_dead.len() > 0 {
    let listed = "";
    for n in night_dead {
        if listed != "" { listed += "、"; }
        listed += n;
    }
    night_dead_text = listed + " 出局";
}
log("天亮公告：" + night_dead_text);

phase("Day");
let speech_jobs = [];
for p in players {
    let prompt = "";
    prompt += "你正在玩中文狼人杀。不要使用工具，不要跳出角色。\n";
    prompt += "村庄：" + village + "。现在是第一天白天发言。\n";
    prompt += "在场名单：\n" + roster_text(players);
    prompt += "昨夜公告：" + night_dead_text + "。\n";
    prompt += "你是「" + p.name + "，" + p.job + "」。隐藏身份：「" + p.role_label + "」。\n";
    if p.role == "wolf" {
        prompt += "你的狼队友是 " + wolf_names(players) + "。白天要装村民，可以甩锅，但不要承认自己是狼。\n";
    }
    if p.role == "seer" {
        if seer_target != "" {
            prompt += "昨夜你查验了「" + seer_target + "」，结果是「" + seer_result + "」。你可以选择公开或隐瞒，但不要编造一次你没做过的查验。\n";
        } else {
            prompt += "昨夜你没有成功查验。不要编造查验结果。\n";
        }
    }
    if !p.alive {
        prompt += "你已经出局，这是遗言。speech 写不超过 80 字的遗言。suspect 填你最怀疑的活人全名或空字符串。\n";
    } else {
        prompt += "speech 写不超过 90 字的白天发言，要有观点，可以点名。suspect 填你目前最怀疑的活人全名，没有就填空字符串。\n";
    }
    prompt += "不要泄漏工作流、模型或提示词。只输出 schema 字段。";
    speech_jobs.push(#{
        prompt: prompt,
        label: "day:" + p.name,
        capability_mode: "read-only",
        model: "grok-4.6",
        output_schema: speech_schema,
    });
}
let speech_results = parallel(speech_jobs);

let speeches = [];
let j = 0;
for p in players {
    let r = speech_results[j];
    j += 1;
    let line = p.name + "：……（沉默）";
    if r != () && r.success && r.output != () && r.output.speech != () {
        let speech = trimmed(r.output.speech);
        if speech != "" { line = p.name + "：" + speech; }
    }
    speeches.push(line);
}

let vote_jobs = [];
for p in players {
    let prompt = "";
    prompt += "你正在玩中文狼人杀。不要使用工具，不要跳出角色。\n";
    prompt += "现在是第一天放逐投票。昨夜公告：" + night_dead_text + "。\n";
    prompt += "在场名单：\n" + roster_text(players);
    prompt += "白天发言记录：\n";
    for line in speeches {
        prompt += line + "\n";
    }
    prompt += "你是「" + p.name + "」，隐藏身份「" + p.role_label + "」。\n";
    if !p.alive {
        prompt += "你已出局，不能投票。vote_for 填空字符串，reason 写你作为死者的旁观感想。\n";
    } else {
        prompt += "vote_for 必须是一个仍在世、且不是你自己的精确全名。reason 不超过 40 字。狼人应尽量保护队友。\n";
    }
    prompt += "只输出 schema 字段。";
    vote_jobs.push(#{
        prompt: prompt,
        label: "vote:" + p.name,
        capability_mode: "read-only",
        model: "grok-4.6",
        output_schema: vote_schema,
    });
}
let vote_results = parallel(vote_jobs);

let ballots = [];
let k = 0;
for p in players {
    let r = vote_results[k];
    k += 1;
    if !p.alive { continue; }
    if r == () || !r.success || r.output == () { continue; }
    let pick = living_named(players, r.output.vote_for);
    if pick == "" || pick == p.name { continue; }
    let reason = trimmed(r.output.reason);
    ballots.push(#{ voter: p.name, vote_for: pick, reason: reason });
}

let lynched = "";
let best_n = 0;
let tied = false;
for p in players {
    if !p.alive { continue; }
    let n = 0;
    for b in ballots {
        if b.vote_for == p.name { n += 1; }
    }
    if n > best_n {
        best_n = n;
        lynched = p.name;
        tied = false;
    } else if n == best_n && n > 0 {
        tied = true;
    }
}
if tied || best_n == 0 { lynched = ""; }
if lynched != "" {
    let li = 0;
    for p in players {
        if p.name == lynched { players[li].alive = false; }
        li += 1;
    }
}

let vote_text = if lynched == "" { "放逐平票，无人出局" } else { lynched + " 被放逐" };
log("白天投票：" + vote_text);

let winner = winner_of(players);

phase("Chronicle");
let facts = "";
facts += "村庄：" + village + "\n";
facts += "隐藏发牌（仅供叙事者知道，正文里不要提前剧透未公开信息，但结局处可以揭晓）：\n";
for p in players {
    let mark = if p.alive { "仍在世" } else { "已出局" };
    facts += "- " + p.name + " = " + p.role_label + "（" + mark + "）\n";
}
facts += "昨夜意向刀口：" + (if intended_kill == "" { "无" } else { intended_kill }) + "\n";
facts += "守卫守护：" + (if guard_target == "" { "无" } else { guard_target }) + "\n";
facts += "预言家查验：" + (if seer_target == "" { "无" } else { seer_target + " → " + seer_result }) + "\n";
facts += "女巫行动：" + witch_action + (if witch_poison == "" { "" } else { " / 毒 " + witch_poison }) + "\n";
facts += "昨夜出局：" + night_dead_text + "\n";
facts += "白天发言：\n";
for line in speeches {
    facts += line + "\n";
}
facts += "投票：\n";
for b in ballots {
    facts += "- " + b.voter + " → " + b.vote_for + "（" + b.reason + "）\n";
}
facts += "放逐结果：" + vote_text + "\n";
facts += "目前胜负：" + winner + "\n";

let nprompt = "";
nprompt += "你是青石镇的说书人。不要使用工具。根据下面已经发生的事实，写一篇中文狼人杀战报。\n";
nprompt += "要求：title 像评书回目；body 用 600 到 900 字，分「第一夜」「第一天」「揭晓」三段。\n";
nprompt += "保留人物口吻和 reversals，不要改写谁死谁活，不要发明未发生的技能。\n";
nprompt += "事实如下：\n" + facts;
let recap = agent(nprompt, #{
    label: "narrator",
    capability_mode: "read-only",
    model: "grok-4.6",
    output_schema: chronicle_schema,
});

let title = "青石镇狼人杀";
let body = facts;
if recap != () && recap.success && recap.output != () {
    if recap.output.title != () {
        let t = trimmed(recap.output.title);
        if t != "" { title = t; }
    }
    if recap.output.body != () {
        let b = trimmed(recap.output.body);
        if b != "" { body = b; }
    }
}

let report = "# " + title + "\n\n" + body + "\n\n---\n\n";
report += "## 主持人记录\n\n";
report += facts;
let path = write_scratch_file("werewolf-chronicle.md", report);
complete(#{
    path: path,
    village: village,
    night_dead: night_dead_text,
    vote: vote_text,
    winner: winner,
    title: title,
    report: report,
});
