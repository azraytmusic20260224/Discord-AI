import os
import random
import asyncio
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from groq import Groq
from google import genai

# ---------------------------------------------------------
# 1. 設定・初期化
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 🔑 キーの読み込み
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TARGET_CHANNEL_ID_STR = os.getenv("TARGET_CHANNEL_ID")

TARGET_CHANNEL_ID = int(TARGET_CHANNEL_ID_STR) if TARGET_CHANNEL_ID_STR else None

# APIクライアントの設定
groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 状態管理用の辞書
reset_markers = {}     # チャンネルごとのリセット位置（履歴の区切り）
channel_settings = {}  # チャンネルごとのテーマ設定 {channel_id: {"theme": テーマ, "expire": 終了時刻}}
channel_modes = {}     # チャンネルごとの継続モード {"mode": "short" / "long" / "chaos", "expire": 終了時刻}
channel_log_limits = {}# チャンネルごとの履歴読み込み件数 {channel_id: 件数}
silent_modes = {}      # チャンネルごとのサイレントモード {channel_id: True/False}
channel_locks = {}     # チャンネルごとの同時実行を防ぐためのロック
last_reply_times = {}  # チャンネルごとのクールダウン用最終返信時刻

# ---------------------------------------------------------
# 2. ユーティリティ関数（AI応答生成 ＆ 1通送信）
# ---------------------------------------------------------
async def generate_ai_reply_async(system_instruction: str, user_prompt: str, mode: str = "normal") -> str:
    max_tokens = 60
    if mode == "long":
        max_tokens = 250
        system_instruction += " たっぷりと詳細に、少し長めの文章で返答してください。"
    elif mode == "short":
        max_tokens = 35
        system_instruction += " 必ず一言、極めて短く（20文字程度で）返答してください。"
    elif mode == "chaos":
        max_tokens = 80
        system_instruction += " 少しシュールでユーモアのある返しをしてください。"

    def _call_ai():
        try:
            chat_completion = groq_client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=max_tokens
            )
            return chat_completion.choices[0].message.content
        except Exception as groq_err:
            print(f"[Groq エラー] {groq_err} -> Gemini に代行切替します")
            try:
                response = gemini_client.models.generate_content(
                    model="models/gemini-2.5-flash",
                    contents=f"{system_instruction}\n\n{user_prompt}",
                    config={"max_output_tokens": max_tokens}
                )
                return response.text
            except Exception as gemini_err:
                print(f"[Gemini エラー] {gemini_err}")
                raise RuntimeError("両方のAIサービスでエラーが発生しました")

    return await asyncio.to_thread(_call_ai)

async def safe_send_single(channel, text, reply_to=None):
    """
    【重要】スパム判定・アカウントBANを防ぐため、分割連投を完全廃止し、
    必ず1通（最大400文字に制限）で安全に送信する関数。
    """
    if not text:
        return
    
    # 400文字に強制スリム化して1通で送信
    safe_text = text[:400]

    try:
        async with channel.typing():
            await asyncio.sleep(0.5)
            if reply_to:
                await reply_to.reply(safe_text, mention_author=False)
            else:
                await channel.send(safe_text)
    except Exception as e:
        print(f"送信エラー: {e}")

def get_time_settings(hour):
    # 深夜帯（23時〜翌6時）は完全に停止
    if 23 <= hour or hour < 6:
        return {"active": False, "min_wait": 0, "max_wait": 0, "reply_rate": 0}
    elif 6 <= hour < 8:
        return {"active": True, "min_wait": 5, "max_wait": 45, "reply_rate": 0.5}
    elif 8 <= hour < 12:
        return {"active": True, "min_wait": 20, "max_wait": 120, "reply_rate": 0.3}
    elif 12 <= hour < 13:
        return {"active": True, "min_wait": 10, "max_wait": 40, "reply_rate": 0.5}
    elif 13 <= hour < 16:
        return {"active": True, "min_wait": 15, "max_wait": 80, "reply_rate": 0.2}
    elif 16 <= hour < 23:
        return {"active": True, "min_wait": 5, "max_wait": 15, "reply_rate": 0.1}
    return {"active": False, "min_wait": 30, "max_wait": 60, "reply_rate": 0.2}

# ---------------------------------------------------------
# 3. イベント・自動つぶやきループ
# ---------------------------------------------------------
@bot.event
async def on_ready():
    print(f'ログインしました: {bot.user}')
    bot.loop.create_task(kaso_boushi_loop())

async def kaso_boushi_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now()
        
        # 深夜帯チェック
        if 23 <= now.hour or now.hour < 6:
            await asyncio.sleep(1800)
            continue

        settings = get_time_settings(now.hour)
        if not settings["active"]:
            await asyncio.sleep(1800)
            continue

        wait_minutes = random.randint(settings["min_wait"], settings["max_wait"])
        await asyncio.sleep(wait_minutes * 60)

        if TARGET_CHANNEL_ID:
            channel = bot.get_channel(TARGET_CHANNEL_ID)
            if channel:
                if silent_modes.get(channel.id, False):
                    continue

                if channel.id not in channel_locks:
                    channel_locks[channel.id] = asyncio.Lock()

                async with channel_locks[channel.id]:
                    try:
                        theme_info = channel_settings.get(channel.id)
                        theme_prompt = ""
                        if theme_info and datetime.now() < theme_info["expire"]:
                            theme_prompt = f"（現在の強制テーマ：{theme_info['theme']}について絡めて話すこと）"
                        
                        mode_info = channel_modes.get(channel.id)
                        current_mode = "normal"
                        if mode_info and datetime.now() < mode_info["expire"]:
                            current_mode = mode_info["mode"]

                        sys_prompt = f"あなたはDiscordのフランクなメンバーです。日常の軽い独り言やつぶやきを短く一つだけ自然につぶやいてください。{theme_prompt}"
                        reply_text = await generate_ai_reply_async(sys_prompt, "何か新しい話題を振って", mode=current_mode)
                        await safe_send_single(channel, reply_text)
                    except Exception as e:
                        print(f"自発つぶやきエラー: {e}")

# ---------------------------------------------------------
# 4. メッセージ受信イベント（コマンド・応答処理）
# ---------------------------------------------------------
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
        return

    content = message.content.strip()
    now = datetime.now()

    # --- 深夜帯（23時〜6時）の完全ブロック ---
    if 23 <= now.hour or now.hour < 6:
        return

    # 1. !reset コマンド（完全リセット）
    if content == '!reset':
        reset_markers[message.channel.id] = message.created_at
        for d in [channel_settings, channel_modes, channel_log_limits, silent_modes]:
            if message.channel.id in d:
                del d[message.channel.id]
        await message.reply("脳みそ・テーマ・モード・履歴件数・サイレント設定をすべて綺麗にリセットしたよ！🧠✨", mention_author=False)
        return

    # 2. !silent_on / !silent_off コマンド
    if content == '!silent_on':
        silent_modes[message.channel.id] = True
        await message.reply("【サイレントモード：ON】これからは話題に参加せず静かに見守るね！🤫", mention_author=False)
        return
    if content == '!silent_off':
        silent_modes[message.channel.id] = False
        await message.reply("【サイレントモード：OFF】通常通り会話に参加するよ！おしゃべり再開！🗣️✨", mention_author=False)
        return

    # 3. !log_XX コマンド（会話履歴の読み込み件数変更）
    if content.startswith('!log_'):
        try:
            limit_str = content[5:].strip()
            limit_val = int(limit_str)
            if 1 <= limit_val <= 50:
                channel_log_limits[message.channel.id] = limit_val
                await message.reply(f"直近の会話履歴の読み込み件数を **{limit_val}件** に変更したよ！📝", mention_author=False)
            else:
                await message.reply("件数は **1から50の間** で指定してね！例: `!log_10`", mention_author=False)
        except ValueError:
            await message.reply("数字で指定してね！例: `!log_10`", mention_author=False)
        return

    # 4. 各種モードの ON / OFF コマンド
    if content == '!short_on':
        channel_modes[message.channel.id] = {"mode": "short", "expire": now + timedelta(hours=12)}
        await message.reply("【ショートモード：ON】これからの発言は極めて短くするね！", mention_author=False)
        return
    if content == '!short_off':
        if message.channel.id in channel_modes and channel_modes[message.channel.id]["mode"] == "short":
            del channel_modes[message.channel.id]
        await message.reply("【ショートモード：OFF】通常の話す長さに戻したよ！", mention_author=False)
        return

    if content == '!long_on':
        channel_modes[message.channel.id] = {"mode": "long", "expire": now + timedelta(hours=12)}
        await message.reply("【ロングモード：ON】これからはたっぷり詳しく語るマンになるよ！", mention_author=False)
        return
    if content == '!long_off':
        if message.channel.id in channel_modes and channel_modes[message.channel.id]["mode"] == "long":
            del channel_modes[message.channel.id]
        await message.reply("【ロングモード：OFF】通常の話す長さに戻したよ！", mention_author=False)
        return

    if content == '!chaos_on':
        channel_modes[message.channel.id] = {"mode": "chaos", "expire": now + timedelta(hours=12)}
        await message.reply("【カオスモード：ON】……フハハハ、意味不明に狂い出してやるぜ……！！🤪", mention_author=False)
        return
    if content == '!chaos_off':
        if message.channel.id in channel_modes and channel_modes[message.channel.id]["mode"] == "chaos":
            del channel_modes[message.channel.id]
        await message.reply("【カオスモード：OFF】正気を取り戻しました。お騒がせしました！😌", mention_author=False)
        return

    # 5. !cancel コマンド（テーマ設定のみ解除）
    if content == '!cancel':
        if message.channel.id in channel_settings:
            del channel_settings[message.channel.id]
            await message.reply("テーマ縛りを解除したよ！🔓", mention_author=False)
        else:
            await message.reply("現在有効なテーマ縛りはないよ！", mention_author=False)
        return

    # 6. !setting_XX コマンド（24時間テーマ固定）
    if content.startswith('!setting_'):
        theme = content[9:].strip()
        if theme:
            expire_time = now + timedelta(hours=24)
            channel_settings[message.channel.id] = {"theme": theme, "expire": expire_time}
            await message.reply(f"了解！これからの24時間、このチャンネルでは**「{theme}」**についてしか話さないマンになるよ！🔥", mention_author=False)
        else:
            await message.reply("テーマが指定されてないよ！例: `!setting_ラーメン`", mention_author=False)
        return

    # 7. !new コマンド（新たな話題）
    if content == '!new':
        if message.channel.id not in channel_locks:
            channel_locks[message.channel.id] = asyncio.Lock()
        
        mode_info = channel_modes.get(message.channel.id)
        current_mode = mode_info["mode"] if mode_info and now < mode_info["expire"] else "normal"

        async with channel_locks[message.channel.id]:
            async with message.channel.typing():
                try:
                    sys_prompt = "あなたはDiscordのフランクなメンバーです。みんなが盛り上がれるような新しい面白い話題を一つ短く振ってください。"
                    reply_text = await generate_ai_reply_async(sys_prompt, "新しい話題を振って", mode=current_mode)
                    await safe_send_single(message.channel, reply_text, reply_to=message)
                except Exception as e:
                    await message.reply(f"エラー: {e}", mention_author=False)
        return

    # --- サイレントモード時は返信判定を行わない ---
    if silent_modes.get(message.channel.id, False):
        return

    # --- 返信判定ロジック ---
    prompt = content
    if content.startswith('!ai '):
        prompt = content[4:].strip()

    is_command = content.startswith('!ai ')
    is_mentioned = bot.user.mentioned_in(message)
    
    is_reply_to_bot = False
    if message.reference and message.reference.resolved:
        if message.reference.resolved.author == bot.user:
            is_reply_to_bot = True

    # ⏱️ クールダウン判定（最低25秒間隔で連投を防止）
    cooldown_seconds = 25
    last_time = last_reply_times.get(message.channel.id)
    is_cooldown_active = last_time and (now - last_time).total_seconds() < cooldown_seconds

    settings = get_time_settings(now.hour)

    should_auto_reply = False
    if settings["active"] and not is_cooldown_active:
        if random.random() < settings["reply_rate"]:
            should_auto_reply = True

    should_reply = is_command or is_mentioned or is_reply_to_bot or should_auto_reply

    if not should_reply:
        return

    # チャンネルごとのロック
    if message.channel.id not in channel_locks:
        channel_locks[message.channel.id] = asyncio.Lock()

    async with channel_locks[message.channel.id]:
        if should_auto_reply and not (is_command or is_mentioned or is_reply_to_bot):
            try:
                async for latest_msg in message.channel.history(limit=1):
                    if latest_msg.id != message.id:
                        return
            except Exception:
                pass

        # テーマ・モード確認
        theme_info = channel_settings.get(message.channel.id)
        theme_prompt = ""
        if theme_info and now < theme_info["expire"]:
            theme_prompt = f"【絶対の縛りルール】現在このチャンネルでは「{theme_info['theme']}」というテーマが設定されています。どんな質問であっても強引に「{theme_info['theme']}」に関連付けて話してください。"
        else:
            if message.channel.id in channel_settings:
                del channel_settings[message.channel.id]

        mode_info = channel_modes.get(message.channel.id)
        current_mode = "normal"
        if mode_info and now < mode_info["expire"]:
            current_mode = mode_info["mode"]
        else:
            if message.channel.id in channel_modes:
                del channel_modes[message.channel.id]

        # --- 【重要】会話履歴の取得（「!」から始まるコマンドやログは完全に除外する） ---
        log_limit = channel_log_limits.get(message.channel.id, 10)
        history_logs = []
        reset_time = reset_markers.get(message.channel.id)

        fetch_limit = max(60, log_limit * 4)
        async for msg in message.channel.history(limit=fetch_limit):
            if reset_time and msg.created_at < reset_time:
                break
            if msg.author == bot.user:
                continue
            
            # 💡 「!」から始まるメッセージ（コマンドやシステムログ等）はAIへの入力・履歴から完全に除外
            if msg.content.strip().startswith('!'):
                continue

            history_logs.append(f"{msg.author.display_name}: {msg.content}")
            if len(history_logs) >= log_limit:
                break
        
        history_logs.reverse()
        context_text = "\n".join(history_logs)

        try:
            sys_prompt = f"あなたはDiscordサーバーの少し変わったメンバーです。日本語で短く自然に会話してください。\n{theme_prompt}"
            full_prompt = f"【直近の会話履歴】\n{context_text}\n\n【ユーザーからの入力/質問】\n{prompt}"
            
            reply_text = await generate_ai_reply_async(sys_prompt, full_prompt, mode=current_mode)
            await safe_send_single(message.channel, reply_text, reply_to=message)
            
            # クールダウン用タイムスタンプ更新
            last_reply_times[message.channel.id] = datetime.now()
            
        except Exception as e:
            print(f"返信エラー: {e}")
            if is_command:
                await message.reply(f"エラーになっちゃった…！(詳細: {e})", mention_author=False)

# ---------------------------------------------------------
# ボットの起動
# ---------------------------------------------------------
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("エラー: DISCORD_TOKEN が設定されていません。")
