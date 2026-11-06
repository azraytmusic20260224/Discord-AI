import os
import random
import asyncio
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from groq import Groq
from google import genai

# インテントの設定
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
channel_locks = {}     # チャンネルごとの同時実行を防ぐためのロック
last_reply_times = {}  # チャンネルごとのクールダウン用最終返信時刻

# 💡 AI呼び出し関数（モードに応じて指示や長さを調整）
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
        system_instruction += " 意味不明で支離滅裂な、完全に文脈が崩壊したカオスな文章を出力してください。"

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

# 💡 分割送信＆リプライ対応関数
async def send_split_message(channel, text, chunk_size=45, reply_to=None):
    if not text:
        return
    
    if len(text) <= chunk_size:
        async with channel.typing():
            await asyncio.sleep(0.6)
            if reply_to:
                await reply_to.reply(text, mention_author=False)
            else:
                await channel.send(text)
        return

    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks):
        async with channel.typing():
            await asyncio.sleep(1.0)
            if i == 0 and reply_to:
                await reply_to.reply(chunk, mention_author=False)
            else:
                await channel.send(chunk)

@bot.event
async def on_ready():
    print(f'ログインしました: {bot.user}')
    bot.loop.create_task(kaso_boushi_loop())

def get_time_settings(hour):
    if 23 <= hour or hour < 6:
        return {"active": False, "min_wait": 0, "max_wait": 0, "reply_rate": 0}
    elif 6 <= hour < 8:
        return {"active": True, "min_wait": 15, "max_wait": 30, "reply_rate": 0.3}
    elif 8 <= hour < 12:
        return {"active": True, "min_wait": 30, "max_wait": 90, "reply_rate": 0.5}
    elif 12 <= hour < 13:
        return {"active": True, "min_wait": 20, "max_wait": 40, "reply_rate": 0.4}
    elif 13 <= hour < 16:
        return {"active": True, "min_wait": 30, "max_wait": 80, "reply_rate": 0.5}
    elif 16 <= hour < 23:
        return {"active": True, "min_wait": 15, "max_wait": 40, "reply_rate": 0.2}
    return {"active": False, "min_wait": 30, "max_wait": 60, "reply_rate": 0.3}

# 💡 自動つぶやきループ
async def kaso_boushi_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now()
        settings = get_time_settings(now.hour)

        if not settings["active"]:
            await asyncio.sleep(1800)
            continue

        wait_minutes = random.randint(settings["min_wait"], settings["max_wait"])
        await asyncio.sleep(wait_minutes * 60)

        if TARGET_CHANNEL_ID:
            channel = bot.get_channel(TARGET_CHANNEL_ID)
            if channel:
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

                        sys_prompt = f"あなたはDiscordのフランクなメンバーです。日常の軽い独り言やつぶやきを一つだけ自然につぶやいてください。{theme_prompt}"
                        reply_text = await generate_ai_reply_async(sys_prompt, "何か新しい話題を振って", mode=current_mode)
                        await send_split_message(channel, reply_text)
                    except Exception as e:
                        print(f"自発つぶやきエラー: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
        return

    content = message.content.strip()
    now = datetime.now()

    # 1. !reset コマンド（完全リセット）
    if content == '!reset':
        reset_markers[message.channel.id] = message.created_at
        if message.channel.id in channel_settings:
            del channel_settings[message.channel.id]
        if message.channel.id in channel_modes:
            del channel_modes[message.channel.id]
        if message.channel.id in channel_log_limits:
            del channel_log_limits[message.channel.id]
        await message.reply("脳みそ・テーマ・モード・履歴件数設定をすべて綺麗にリセットしたよ！🧠✨", mention_author=False)
        return

    # 2. !log_XX コマンド（会話履歴の読み込み件数変更）
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

    # 3. 各種モードの ON / OFF コマンド
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

    # 4. !cancel コマンド（テーマ設定のみ解除）
    if content == '!cancel':
        if message.channel.id in channel_settings:
            del channel_settings[message.channel.id]
            await message.reply("テーマ縛りを解除したよ！🔓", mention_author=False)
        else:
            await message.reply("現在有効なテーマ縛りはないよ！", mention_author=False)
        return

    # 5. !setting_XX コマンド（24時間テーマ固定）
    if content.startswith('!setting_'):
        theme = content[9:].strip()
        if theme:
            expire_time = now + timedelta(hours=24)
            channel_settings[message.channel.id] = {"theme": theme, "expire": expire_time}
            await message.reply(f"了解！これからの24時間、このチャンネルでは**「{theme}」**についてしか話さないマンになるよ！🔥", mention_author=False)
        else:
            await message.reply("テーマが指定されてないよ！例: `!setting_ラーメン`", mention_author=False)
        return

    # 6. !new コマンド（新たな話題）
    if content == '!new':
        if message.channel.id not in channel_locks:
            channel_locks[message.channel.id] = asyncio.Lock()
        
        mode_info = channel_modes.get(message.channel.id)
        current_mode = mode_info["mode"] if mode_info and now < mode_info["expire"] else "normal"

        async with channel_locks[message.channel.id]:
            async with message.channel.typing():
                try:
                    sys_prompt = "あなたはDiscordのフランクなメンバーです。みんなが盛り上がれるような新しい面白い話題を一つ振ってください。"
                    reply_text = await generate_ai_reply_async(sys_prompt, "新しい話題を振って", mode=current_mode)
                    await send_split_message(message.channel, reply_text, reply_to=message)
                except Exception as e:
                    await message.reply(f"エラー: {e}", mention_author=False)
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

    # ⏱️ クールダウン判定（最低20秒間隔）
    cooldown_seconds = 20
    last_time = last_reply_times.get(message.channel.id)
    is_cooldown_active = last_time and (now - last_time).total_seconds() < cooldown_seconds

    settings = get_time_settings(now.hour)

    # 確率的自動返信の判定
    # 条件: 時間帯が有効かつ、クールダウン（20秒）が明けており、かつ確率にヒットする場合のみ
    should_auto_reply = False
    if settings["active"] and not is_cooldown_active:
        if random.random() < settings["reply_rate"]:
            should_auto_reply = True

    # 最終的に返信すべきかどうか（メンションやコマンド等は無条件、自動返信は上記フラグに依存）
    should_reply = is_command or is_mentioned or is_reply_to_bot or should_auto_reply

    if not should_reply:
        return

    # チャンネルごとのロック（多重実行やAPI負荷の防止）
    if message.channel.id not in channel_locks:
        channel_locks[message.channel.id] = asyncio.Lock()

    # ロックを取得して処理を実行（すでに別の返信処理が走っている場合はスキップ・待機して最新メッセージに絞る）
    async with channel_locks[message.channel.id]:
        # 【追加対策】ロック取得時点において、直前のメッセージがこのメッセージより新しいものであれば
        # 多重処理や古いメッセージへの過剰返信を防ぐため、自動返信の場合はここでスキップする
        if should_auto_reply and not (is_command or is_mentioned or is_reply_to_bot):
            try:
                async for latest_msg in message.channel.history(limit=1):
                    if latest_msg.id != message.id:
                        # 自分以外のより新しいメッセージが既に存在している場合は重複を防ぐため中止
                        return
            except Exception:
                pass

        # テーマ設定の確認
        theme_info = channel_settings.get(message.channel.id)
        theme_prompt = ""
        if theme_info and now < theme_info["expire"]:
            theme_prompt = f"【絶対の縛りルール】現在このチャンネルでは「{theme_info['theme']}」というテーマが設定されています。どんな質問や話題であっても、無理やり・強引に「{theme_info['theme']}」に関連付けて話してください。"
        else:
            if message.channel.id in channel_settings:
                del channel_settings[message.channel.id]

        # 継続モードの確認
        mode_info = channel_modes.get(message.channel.id)
        current_mode = "normal"
        if mode_info and now < mode_info["expire"]:
            current_mode = mode_info["mode"]
        else:
            if message.channel.id in channel_modes:
                del channel_modes[message.channel.id]

        # 直近の会話履歴を取得（チャンネルごとの設定値を適用。デフォルトは10件）
        log_limit = channel_log_limits.get(message.channel.id, 10)
        history_logs = []
        reset_time = reset_markers.get(message.channel.id)

        fetch_limit = max(50, log_limit * 3)
        async for msg in message.channel.history(limit=fetch_limit):
            if reset_time and msg.created_at < reset_time:
                break
            if msg.author == bot.user:
                continue
            history_logs.append(f"{msg.author.display_name}: {msg.content}")
            if len(history_logs) >= log_limit:
                break
        
        history_logs.reverse()
        context_text = "\n".join(history_logs)

        async with message.channel.typing():
            try:
                sys_prompt = f"あなたはDiscordサーバーの少し変わったメンバーです。日本語で自然に会話してください。\n{theme_prompt}"
                full_prompt = f"【直近の会話履歴】\n{context_text}\n\n【ユーザーからの入力/質問】\n{prompt}"
                
                reply_text = await generate_ai_reply_async(sys_prompt, full_prompt, mode=current_mode)
                await send_split_message(message.channel, reply_text, reply_to=message)
                
                # クールダウン用タイムスタンプ更新
                last_reply_times[message.channel.id] = datetime.now()
                
            except Exception as e:
                print(f"返信エラー: {e}")
                if is_command:
                    await message.reply(f"エラーになっちゃった…！(詳細: {e})", mention_author=False)

# ボットの起動
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("エラー: DISCORD_TOKEN が設定されていません。")
