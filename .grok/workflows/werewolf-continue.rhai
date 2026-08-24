let meta = #{
    name: "werewolf-continue",
    description: "Resume the Qingshi werewolf table from day-1 exile and play up to two more nights and days",
    when_to_use: "When an in-progress werewolf run should continue from the saved seating",
    phases: [
        #{ title: "Night Two", detail: "living seats take night two actions" },
        #{ title: "Day Two", detail: "day two speeches and exile vote" },
        #{ title: "Night Three", detail: "living seats take night three actions if needed" },
        #{ title: "Day Three", detail: "day three speeches and exile vote if needed" },
        #{ title: "Chronicle", detail: "narrator writes the continuation" },
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
let hunter_schema = #{
    "type": "object",
    "required": ["shot", "reason"],
    "properties": #{
        "shot": #{ "type": "string" },
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

fn names_line_of(players) {
    let text = "";
    for p in players {
        if text != "" { text += "、"; }
        text += p.name;
    }
    text
}

fn join_list(items) {
    let text = "";
    for x in items {
        if text != "" { text += "、"; }
        text += x;
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

fn mark_dead(players, name) {
    let i = 0;
    for p in players {
        if p.name == name { players[i].alive = false; }
        i += 1;
    }
    players
}

fn is_alive(players, name) {
    for p in players {
        if p.name == name { return p.alive; }
    }
    false
}

let village = "青石镇";
if args != () && args.village != () {
    let custom = trimmed(args.village);
    if custom != "" { village = custom; }
}

// Seat the table exactly where game 1 stopped.
let players = [
    #{ name: "村长老张", job: "村长", role: "villager", role_label: "村民", camp: "good", alive: true },
    #{ name: "铁匠阿强", job: "铁匠", role: "wolf", role_label: "狼人", camp: "wolf", alive: true },
    #{ name: "医生林晚", job: "大夫", role: "seer", role_label: "预言家", camp: "good", alive: true },
    #{ name: "教师苏晴", job: "教师", role: "villager", role_label: "村民", camp: "good", alive: true },
    #{ name: "猎户周山", job: "猎户", role: "hunter", role_label: "猎人", camp: "good", alive: true },
    #{ name: "寡妇陈姨", job: "药铺老板", role: "witch", role_label: "女巫", camp: "good", alive: true },
    #{ name: "少年小满", job: "学徒", role: "wolf", role_label: "狼人", camp: "wolf", alive: true },
    #{ name: "旅人北风", job: "外乡人", role: "villager", role_label: "村民", camp: "good", alive: false },
    #{ name: "厨师老白", job: "厨子", role: "guard", role_label: "守卫", camp: "good", alive: true },
    #{ name: "守夜人灰", job: "更夫", role: "villager", role_label: "村民", camp: "good", alive: true },
];

let used_save = true;
let used_poison = false;
let last_guard = "村长老张";
let public_history = "";
public_history += "已发生：第一夜狼人刀医生林晚，守卫守村长老张，女巫用解药，平安夜。\n";
public_history += "预言家林晚白天跳身份，查验旅人北风为金水。\n";
public_history += "第一天放逐：旅人北风（村民）以 6 票出局。票型北风6、灰2、周山1、陈姨1。\n";
public_history += "当前仍在世：老张、阿强、林晚、苏晴、周山、陈姨、小满、老白、灰。\n";

let facts = public_history;
let names_line = names_line_of(players);
log("续局开桌：从第一天放逐北风之后继续，最多再打两夜两天。");

let winner = winner_of(players);
let round_names = ["第二", "第三"];
let ri = 0;
for round_name in round_names {
    if winner != "尚未分出胜负" { break; }

    let night_title = if ri == 0 { "Night Two" } else { "Night Three" };
    let day_title = if ri == 0 { "Day Two" } else { "Day Three" };
    phase(night_title);

    let night_jobs = [];
    for p in players {
        if !p.alive { continue; }
        let prompt = "";
        prompt += "你正在续玩中文狼人杀。不要使用工具，不要跳出角色。\n";
        prompt += "村庄：" + village + "。现在是" + round_name + "夜。\n";
        prompt += public_history;
        prompt += "在场名单：\n" + roster_text(players);
        prompt += "你是「" + p.name + "，" + p.job + "」。隐藏身份：「" + p.role_label + "」。\n";
        prompt += "合法活人全名必须精确使用。全体名单：" + names_line + "\n";
        if p.role == "wolf" {
            prompt += "狼队友：" + wolf_names(players) + "。不能刀自己人。林晚已公开跳预言家。\n";
            prompt += "action 填 kill，target 填一个在世好人全名。public_dream 不要泄漏身份。\n";
        } else if p.role == "seer" {
            prompt += "你已公开跳预言家，昨夜验过北风是金水。今晚再验一个在世人。\n";
            prompt += "action 填 inspect，target 填全名。不要编造你没做过的查验。\n";
        } else if p.role == "guard" {
            prompt += "你昨夜守护了「" + last_guard + "」，今晚不能连续守护同一个人。\n";
            prompt += "action 填 protect，target 填另一个在世全名。\n";
        } else if p.role == "witch" {
            prompt += "解药已用完。毒药" + (if used_poison { "已用完" } else { "还在" }) + "。这一步先 wait。\n";
            prompt += "action 填 wait，target 填空字符串。\n";
        } else {
            prompt += "你没有夜间技能。action 填 sleep，target 填空字符串。public_dream 不超过 40 字。\n";
        }
        prompt += "只输出 schema 字段，中文短句。";
        night_jobs.push(#{
            prompt: prompt,
            label: "n" + (ri + 2).to_string() + ":" + p.name,
            capability_mode: "read-only",
            model: "grok-4.6",
            output_schema: night_schema,
        });
    }
    let night_results = [];
    if night_jobs.len() > 0 { night_results = parallel(night_jobs); }

    let wolf_targets = [];
    let seer_target = "";
    let guard_target = "";
    let ni = 0;
    for p in players {
        if !p.alive { continue; }
        let r = ();
        if ni < night_results.len() { r = night_results[ni]; }
        ni += 1;
        if r == () || !r.success || r.output == () { continue; }
        let action = trimmed(r.output.action);
        let target = living_named(players, r.output.target);
        if p.role == "wolf" && action == "kill" && target != "" && camp_of(players, target) != "wolf" {
            wolf_targets.push(target);
        }
        if p.role == "seer" && action == "inspect" { seer_target = target; }
        if p.role == "guard" && action == "protect" && target != last_guard { guard_target = target; }
    }

    let intended_kill = "";
    if wolf_targets.len() > 0 {
        intended_kill = wolf_targets[0];
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
        seer_result = if camp_of(players, seer_target) == "wolf" { "狼人" } else { "好人" };
    }

    let witch_action = "none";
    let witch_poison = "";
    let witch_player = ();
    for p in players {
        if p.role == "witch" && p.alive { witch_player = p; }
    }
    if witch_player != () && !used_poison {
        let wprompt = "";
        wprompt += "你正在续玩中文狼人杀。不要使用工具。你是女巫「" + witch_player.name + "」。\n";
        wprompt += public_history;
        wprompt += "解药已用完，毒药还在。今晚狼人准备刀：「" + (if intended_kill == "" { "无人" } else { intended_kill }) + "」。\n";
        wprompt += "在场：\n" + roster_text(players);
        wprompt += "action 只能是 poison 或 none。poison 时 target 必须是在世且不是你自己的全名。none 时 target 为空。\n";
        let wr = agent(wprompt, #{
            label: "witch-" + round_name,
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
    if intended_kill != "" && !wolf_blocked {
        night_dead.push(intended_kill);
    }
    if witch_action == "poison" && witch_poison != "" {
        let already = false;
        for n in night_dead {
            if n == witch_poison { already = true; }
        }
        if !already { night_dead.push(witch_poison); }
        used_poison = true;
    }
    for n in night_dead {
        players = mark_dead(players, n);
    }

    let hunter_shot = "";
    if !is_alive(players, "猎户周山") {
        let hprompt = "";
        hprompt += "你是猎户周山，隐藏身份猎人。你今夜出局，必须开枪带走一个仍在世的人。\n";
        hprompt += "在场：\n" + roster_text(players);
        hprompt += "shot 填精确全名。reason 不超过 40 字。不要使用工具。\n";
        let hr = agent(hprompt, #{
            label: "hunter-night-" + round_name,
            capability_mode: "read-only",
            model: "grok-4.6",
            output_schema: hunter_schema,
        });
        if hr != () && hr.success && hr.output != () {
            hunter_shot = living_named(players, hr.output.shot);
            if hunter_shot != "" {
                players = mark_dead(players, hunter_shot);
                night_dead.push(hunter_shot);
            }
        }
    }

    if guard_target != "" { last_guard = guard_target; }
    let night_dead_text = if night_dead.len() == 0 { "平安夜，无人出局" } else { join_list(night_dead) + " 出局" };
    log(round_name + "夜公告：" + night_dead_text);
    facts += round_name + "夜刀口：" + (if intended_kill == "" { "无" } else { intended_kill });
    facts += "；守卫：" + (if guard_target == "" { "无" } else { guard_target });
    facts += "；查验：" + (if seer_target == "" { "无" } else { seer_target + " → " + seer_result });
    facts += "；女巫：" + witch_action;
    if witch_poison != "" { facts += "/" + witch_poison; }
    if hunter_shot != "" { facts += "；猎人带走 " + hunter_shot; }
    facts += "；出局：" + night_dead_text + "\n";
    public_history += round_name + "夜公告：" + night_dead_text + "。\n";
    if seer_target != "" {
        // Keep seer result private except she may reveal it herself by day.
        facts += "";
    }

    winner = winner_of(players);
    if winner != "尚未分出胜负" {
        log("天亮前已分胜负：" + winner);
        break;
    }

    phase(day_title);
    let speech_jobs = [];
    for p in players {
        if !p.alive { continue; }
        let prompt = "";
        prompt += "你正在续玩中文狼人杀。不要使用工具，不要跳出角色。\n";
        prompt += "现在是" + round_name + "天白天发言。\n";
        prompt += public_history;
        prompt += "昨夜公告：" + night_dead_text + "。\n";
        prompt += "在场：\n" + roster_text(players);
        prompt += "你是「" + p.name + "，" + p.job + "」。隐藏身份：「" + p.role_label + "」。\n";
        if p.role == "wolf" {
            prompt += "狼队友：" + wolf_names(players) + "。装村民，不要承认是狼。林晚已跳预言家。\n";
        }
        if p.role == "seer" {
            if seer_target != "" {
                prompt += "今晚你查验了「" + seer_target + "」，结果是「" + seer_result + "」。可公开或隐瞒，不要编造没做过的查验。\n";
            } else {
                prompt += "今晚没有成功查验。不要编造查验。\n";
            }
        }
        prompt += "speech 不超过 90 字，要有观点。suspect 填最怀疑的在世全名或空字符串。\n";
        speech_jobs.push(#{
            prompt: prompt,
            label: "d" + (ri + 2).to_string() + ":" + p.name,
            capability_mode: "read-only",
            model: "grok-4.6",
            output_schema: speech_schema,
        });
    }
    let speech_results = [];
    if speech_jobs.len() > 0 { speech_results = parallel(speech_jobs); }

    let speeches = [];
    let sj = 0;
    for p in players {
        if !p.alive { continue; }
        let line = p.name + "：……（沉默）";
        let r = ();
        if sj < speech_results.len() { r = speech_results[sj]; }
        sj += 1;
        if r != () && r.success && r.output != () && r.output.speech != () {
            let speech = trimmed(r.output.speech);
            if speech != "" { line = p.name + "：" + speech; }
        }
        speeches.push(line);
        facts += line + "\n";
    }

    let vote_jobs = [];
    for p in players {
        if !p.alive { continue; }
        let prompt = "";
        prompt += "你正在续玩中文狼人杀。不要使用工具。现在是" + round_name + "天放逐投票。\n";
        prompt += public_history + "昨夜公告：" + night_dead_text + "。\n";
        prompt += "白天发言：\n";
        for line in speeches {
            prompt += line + "\n";
        }
        prompt += "你是「" + p.name + "」，隐藏身份「" + p.role_label + "」。\n";
        prompt += "vote_for 必须是在世且不是你自己的精确全名。reason 不超过 40 字。\n";
        vote_jobs.push(#{
            prompt: prompt,
            label: "v" + (ri + 2).to_string() + ":" + p.name,
            capability_mode: "read-only",
            model: "grok-4.6",
            output_schema: vote_schema,
        });
    }
    let vote_results = [];
    if vote_jobs.len() > 0 { vote_results = parallel(vote_jobs); }

    let ballots = [];
    let vk = 0;
    for p in players {
        if !p.alive { continue; }
        let r = ();
        if vk < vote_results.len() { r = vote_results[vk]; }
        vk += 1;
        if r == () || !r.success || r.output == () { continue; }
        let pick = living_named(players, r.output.vote_for);
        if pick == "" || pick == p.name { continue; }
        ballots.push(#{ voter: p.name, vote_for: pick, reason: trimmed(r.output.reason) });
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
    if lynched != "" { players = mark_dead(players, lynched); }

    let day_hunter_shot = "";
    if lynched == "猎户周山" {
        let hprompt = "";
        hprompt += "你是猎户周山，猎人被放逐，必须开枪带走一个仍在世的人。\n";
        hprompt += "在场：\n" + roster_text(players);
        hprompt += "shot 填精确全名。不要使用工具。\n";
        let hr = agent(hprompt, #{
            label: "hunter-day-" + round_name,
            capability_mode: "read-only",
            model: "grok-4.6",
            output_schema: hunter_schema,
        });
        if hr != () && hr.success && hr.output != () {
            day_hunter_shot = living_named(players, hr.output.shot);
            if day_hunter_shot != "" { players = mark_dead(players, day_hunter_shot); }
        }
    }

    let vote_text = if lynched == "" { "放逐平票，无人出局" } else { lynched + " 被放逐" };
    if day_hunter_shot != "" { vote_text += "；猎人带走 " + day_hunter_shot; }
    log(round_name + "天投票：" + vote_text);
    facts += round_name + "天投票：\n";
    for b in ballots {
        facts += "- " + b.voter + " → " + b.vote_for + "（" + b.reason + "）\n";
    }
    facts += "放逐结果：" + vote_text + "\n";
    public_history += round_name + "天：" + vote_text + "。\n";

    winner = winner_of(players);
    ri += 1;
}

phase("Chronicle");
facts += "最终胜负：" + winner + "\n";
facts += "最终存活：\n" + roster_text(players);

let nprompt = "";
nprompt += "你是青石镇说书人。不要使用工具。这是狼人杀续局战报。\n";
nprompt += "title 像评书回目，点明第二回。body 用 700 到 1000 字，按夜/天分段，最后揭晓胜负。\n";
nprompt += "不要改写谁死谁活，不要发明没发生的技能。事实：\n" + facts;
let recap = agent(nprompt, #{
    label: "narrator",
    capability_mode: "read-only",
    model: "grok-4.6",
    output_schema: chronicle_schema,
});

let title = "第二回 青石镇续命";
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

let report = "# " + title + "\n\n" + body + "\n\n---\n\n## 主持人记录\n\n" + facts;
let path = write_scratch_file("werewolf-chronicle-2.md", report);
complete(#{
    path: path,
    village: village,
    winner: winner,
    title: title,
    report: report,
});
