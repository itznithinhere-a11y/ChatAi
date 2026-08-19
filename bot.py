import os,re,ast,operator,random,time,threading,tempfile,logging
from collections import deque
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from cachetools import TTLCache
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask
from gtts import gTTS
from pydub import AudioSegment
import speech_recognition as sr
import telebot
from telebot import types

# ================= CONFIG =================
def env_int(k,d=0):
    try:return int(os.getenv(k,str(d)).strip())
    except:return d
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); AI_API_KEY=os.getenv('AI_API_KEY','').strip()
AI_BASE_URL=os.getenv('AI_BASE_URL','').strip().rstrip('/'); AI_MODEL=os.getenv('AI_MODEL','').strip()
SUPABASE_URL=os.getenv('SUPABASE_URL','').strip().rstrip('/'); SUPABASE_KEY=os.getenv('SUPABASE_KEY','').strip()
ADMIN_ID=env_int('ADMIN_ID',0); PORT=env_int('PORT',10000)
ENABLE_TTS=os.getenv('ENABLE_TTS','true').lower() in {'1','true','yes','on'}
RESPOND_IN_GROUPS=os.getenv('RESPOND_IN_GROUPS','true').lower() in {'1','true','yes','on'}
AI_TIMEOUT=max(8,env_int('AI_TIMEOUT',18)); AI_RETRIES=2
for n,v in [('BOT_TOKEN',BOT_TOKEN),('AI_API_KEY',AI_API_KEY),('AI_BASE_URL',AI_BASE_URL),('AI_MODEL',AI_MODEL),('SUPABASE_URL',SUPABASE_URL),('SUPABASE_KEY',SUPABASE_KEY)]:
    if not v: raise RuntimeError(f'{n} environment variable is missing.')

# ================= LOGGING / HTTP =================
logging.basicConfig(level=logging.INFO,handlers=[RotatingFileHandler('bot.log',maxBytes=5*1024*1024,backupCount=5,encoding='utf-8'),logging.StreamHandler()],format='%(asctime)s [%(levelname)s] %(message)s')
logger=logging.getLogger('venu')
http=requests.Session(); http.mount('https://',HTTPAdapter(pool_connections=20,pool_maxsize=50,max_retries=Retry(total=2,connect=2,read=2,backoff_factor=.4,status_forcelist=[429,502,503,504],allowed_methods=frozenset(['GET','POST','PATCH','DELETE']),raise_on_status=False))); http.mount('http://',HTTPAdapter(pool_connections=20,pool_maxsize=50,max_retries=2))

# ================= TELEGRAM =================
bot=telebot.TeleBot(BOT_TOKEN,parse_mode=None,threaded=True,num_threads=8)
BOT_ID=None; BOT_USERNAME=''
try:
    me=bot.get_me(); BOT_ID=me.id; BOT_USERNAME=(me.username or '').lower(); logger.info('Telegram: @%s (%s)',BOT_USERNAME,BOT_ID)
except Exception: logger.exception('Telegram get_me failed')

# ================= SUPABASE =================
class DB:
    def __init__(self,url,key): self.url=url; self.headers={'apikey':key,'Authorization':f'Bearer {key}','Content-Type':'application/json'}
    def request(self,method,endpoint,payload=None,timeout=7):
        try:
            u=f'{self.url}/rest/v1/{endpoint}'; h=self.headers.copy()
            if method.upper()=='GET': r=http.get(u,headers=h,timeout=timeout)
            elif method.upper()=='POST': h['Prefer']='return=minimal'; r=http.post(u,headers=h,json=payload,timeout=timeout)
            elif method.upper()=='PATCH': r=http.patch(u,headers=h,json=payload,timeout=timeout)
            elif method.upper()=='DELETE': r=http.delete(u,headers=h,timeout=timeout)
            else:return None
            r.raise_for_status(); return r.json() if r.text else None
        except Exception: logger.exception('DB %s %s failed',method,endpoint); return None
db=DB(SUPABASE_URL,SUPABASE_KEY)

# ================= STATE =================
lock=threading.RLock(); memory=TTLCache(maxsize=2000,ttl=1800); registered=TTLCache(maxsize=10000,ttl=86400)
recent_replies={}; last_msg={}; name_time={}; games={}; tts_users=set(); activity={}

# ================= FLASK =================
app=Flask(__name__)
@app.route('/')
def home(): return '🤖 Venu AI online'
@app.route('/health')
def health(): return {'status':'online','bot_id':BOT_ID,'username':BOT_USERNAME,'model':AI_MODEL}
def run_flask():
    try: app.run(host='0.0.0.0',port=PORT,threaded=True)
    except Exception: logger.exception('Flask stopped')

# ================= MEMORY =================
def default_profile(uid,name='Dost'):
    return {'user_id':uid,'name':name or 'Dost','age':'Not specified','favorite_game':'Not specified','favorite_movie':'Not specified','language':'Hinglish','relationship_status':'Not specified','hobbies':'Not specified','current_mood':'Chill','emotional_momentum':'Stable'}
def register_user(uid,username,first_name):
    with lock:
        if uid in registered:return
        registered[uid]=True
    def w():
        try:
            r=http.post(f'{db.url}/rest/v1/users',headers={**db.headers,'Prefer':'resolution=merge-duplicates,return=minimal'},json={'user_id':uid,'username':username,'first_name':first_name,'is_verified':True},timeout=5); r.raise_for_status()
        except Exception: logger.exception('register failed')
    threading.Thread(target=w,daemon=True).start()
def get_memory(uid,name='Dost'):
    with lock:
        if uid in memory:return memory[uid]
    p=db.request('GET',f'user_profiles?user_id=eq.{uid}&limit=1') or []
    profile=p[0] if p else default_profile(uid,name)
    if not p: db.request('POST','user_profiles',profile)
    s=db.request('GET',f'conversation_summary?user_id=eq.{uid}&limit=1') or []
    summary=s[0].get('summary','Ongoing friendly connection.') if s else 'Ongoing friendly connection.'
    rows=db.request('GET',f'messages?user_id=eq.{uid}&order=created_at.desc&limit=12') or []
    hist=[{'role':r.get('role'),'content':r.get('content')} for r in reversed(rows) if r.get('role') in {'user','assistant'} and r.get('content')]
    packet={'profile':profile,'summary':summary,'history':hist[-12:]}
    with lock: memory[uid]=packet
    return packet
def save_message(uid,role,text):
    if not text:return
    with lock:
        p=memory.get(uid)
        if p:
            p['history'].append({'role':role,'content':text}); p['history']=p['history'][-12:]
    threading.Thread(target=lambda:db.request('POST','messages',{'user_id':uid,'role':role,'content':text}),daemon=True).start()
def update_profile(uid,field,value):
    if field not in {'name','age','favorite_game','favorite_movie','language','relationship_status','hobbies','current_mood','emotional_momentum'}:return
    with lock:
        if uid in memory: memory[uid]['profile'][field]=value
    threading.Thread(target=lambda:db.request('PATCH',f'user_profiles?user_id=eq.{uid}',{field:value}),daemon=True).start()
def clear_memory(uid):
    db.request('DELETE',f'messages?user_id=eq.{uid}')
    with lock:
        memory.pop(uid,None); recent_replies.pop(uid,None); games.pop(uid,None); last_msg.pop(uid,None); tts_users.discard(uid)

def daily(uid,game=False):
    def w():
        d=time.strftime('%Y-%m-%d'); rows=db.request('GET',f'daily_stats?user_id=eq.{uid}&date=eq.{d}&limit=1') or []
        if rows:
            r=rows[0]; db.request('PATCH',f'daily_stats?user_id=eq.{uid}&date=eq.{d}',{'messages_sent':int(r.get('messages_sent',0) or 0)+(0 if game else 1),'games_played':int(r.get('games_played',0) or 0)+(1 if game else 0)})
        else: db.request('POST','daily_stats',{'user_id':uid,'date':d,'messages_sent':0 if game else 1,'games_played':1 if game else 0})
    threading.Thread(target=w,daemon=True).start()

# ================= CALCULATOR =================nOPS={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.USub:operator.neg,ast.UAdd:operator.pos}
def se(n):
    if isinstance(n,ast.Constant) and isinstance(n.value,(int,float)):return n.value
    if isinstance(n,ast.BinOp) and type(n.op) in OPS:return OPS[type(n.op)](se(n.left),se(n.right))
    if isinstance(n,ast.UnaryOp) and type(n.op) in OPS:return OPS[type(n.op)](se(n.operand))
    raise ValueError
def calc(x):
    try:
        if not x or len(x)>100 or not re.fullmatch(r'[0-9+*/().\-\s]+',x):return None
        v=se(ast.parse(x,mode='eval').body); return round(v,8) if isinstance(v,float) and not v.is_integer() else v
    except:return None

# ================= AI =================
def mood(t):
    t=t.lower(); sad=['sad','dukhi','udaas','rona','breakup','depress','tension','pareshan','lonely','akela']; angry=['gussa','angry','hate','bakwas']; happy=['mast','awesome','excited','party','jeet','won']
    if any(x in t for x in sad):return 'supportive'
    if any(x in t for x in angry):return 'calm'
    if any(x in t for x in happy):return 'playful'
    return 'chill'
def prompt(profile,summary,text):
    v={'supportive':'Be warm and supportive; no jokes about serious pain.','calm':'Stay calm; do not escalate.','playful':'Be energetic and playful.','chill':'Be casual, witty and relaxed.'}[mood(text)]
    return f'''You are Venu, a smart desi friend chatting on Telegram.\nNatural Hinglish. {v}\nUsually ONE short sentence; maximum TWO short sentences. No lectures unless asked. Do not repeat the question. Do not use the user name every reply. Do not invent facts. Never be randomly rude. Never mention system prompts. Return ONLY reply text.\n\nProfile: name={profile.get("name","Dost")}, game={profile.get("favorite_game","Not specified")}, movie={profile.get("favorite_movie","Not specified")}, hobbies={profile.get("hobbies","Not specified")}, mood={profile.get("current_mood","Chill")}\nContext: {summary}'''
def clean_reply(x):
    x=str(x or '').strip().replace('```',''); x=re.sub(r'^(Venu|Assistant|Bot)\s*:\s*','',x,flags=re.I); x=re.sub(r'[ \t]+',' ',x); parts=re.split(r'(?<=[.!?।])\s+',x); x=' '.join([p for p in parts if p][:2]); return (x[:237].rsplit(' ',1)[0]+'…') if len(x)>240 else x
def similar(x,arr):
    if len(x)<18:return False
    return any(a==x or (len(a)>=18 and SequenceMatcher(None,a.lower(),x.lower()).ratio()>=.88) for a in arr)
def ai(uid,pkt,text):
    msgs=[{'role':'system','content':prompt(pkt['profile'],pkt['summary'],text)}]
    for m in pkt['history'][-12:]:msgs.append({'role':m['role'],'content':m['content']})
    if not (msgs[-1]['get']('role')=='user' and msgs[-1].get('content')==text):msgs.append({'role':'user','content':text})
    headers={'Authorization':f'Bearer {AI_API_KEY}','Content-Type':'application/json'}; err=None
    for attempt in range(2):
        try:
            r=http.post(f'{AI_BASE_URL}/chat/completions',headers=headers,json={'model':AI_MODEL,'messages':msgs,'temperature':.78 if attempt==0 else .86,'max_tokens':100},timeout=(5,AI_TIMEOUT)); r.raise_for_status(); data=r.json(); c=((data.get('choices') or [{}])[0].get('message') or {}).get('content','')
            if isinstance(c,list):c=''.join(i.get('text','') if isinstance(i,dict) else str(i) for i in c)
            c=clean_reply(c)
            if not c:raise ValueError('empty AI reply')
            with lock: rr=recent_replies.setdefault(uid,deque(maxlen=8)); dup=similar(c,rr)
            if dup and attempt==0:
                msgs[0]['content']+='\nUse different wording from your previous reply.'; continue
            with lock: rr.append(c)
            return c,mood(text)
        except Exception as e: err=e; logger.warning('AI attempt %s failed: %s',attempt+1,e); time.sleep(.3)
    logger.exception('AI unavailable: %s',err)
    f={'supportive':'Haan bhai, main yahin hoon. Bol kya hua?','calm':'Haan, bol. Main sun raha hoon.','playful':'Aaja bhai 😎 kya scene hai?','chill':'Haan bhai, bol kya scene hai? 😎'}[mood(text)]
    with lock:recent_replies.setdefault(uid,deque(maxlen=8)).append(f)
    return f,mood(text)

# ================= TYPING =================
class Typing:
    def __init__(self,chat):self.chat=chat;self.stop=threading.Event()
    def start(self):
        self.send(); threading.Thread(target=self.loop,daemon=True).start()
    def send(self):
        try:bot.send_chat_action(self.chat,'typing')
        except:pass
    def loop(self):
        while not self.stop.wait(4):self.send()
    def close(self):self.stop.set()

# ================= MENUS =================
def main_kb():
    k=types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton('💬 Talk',callback_data='talk'),types.InlineKeyboardButton('🎮 Games',callback_data='games'))
    k.add(types.InlineKeyboardButton('🧠 Memory',callback_data='memory'),types.InlineKeyboardButton('👤 Profile',callback_data='profile'))
    k.add(types.InlineKeyboardButton('😂 Fun',callback_data='fun'),types.InlineKeyboardButton('📊 Stats',callback_data='stats'))
    k.add(types.InlineKeyboardButton('🎙️ Voice',callback_data='voice'),types.InlineKeyboardButton('ℹ️ Help',callback_data='help'))
    k.add(types.InlineKeyboardButton('➕ Add To Group',callback_data='group'),types.InlineKeyboardButton('🧹 Clear',callback_data='clear')); return k
def game_kb():
    k=types.InlineKeyboardMarkup(row_width=2); k.add(types.InlineKeyboardButton('🎯 Guess Number',callback_data='guess'),types.InlineKeyboardButton('🎲 Truth or Dare',callback_data='tod')); k.add(types.InlineKeyboardButton('🧩 Riddle',callback_data='riddle'),types.InlineKeyboardButton('🔥 Roast',callback_data='roast')); k.add(types.InlineKeyboardButton('⬅️ Back',callback_data='back')); return k

# ================= FUN =================nJ=['Maine diet start ki thi... phir samose ne aankhon mein aankhein daal di 😭','WiFi slow aur salary khatam dono bina warning ke hote hain 💀','Mera motivation Monday ke saath long-distance relationship mein hai 😂']
SH=['Chai garam, mausam suhana, dost tu mil jaaye toh scene mastana ☕❤️','Zindagi chhoti si hai, tension badi bana rakhi hai. Hans le bhai 😌']

def joke(m):bot.reply_to(m,'😂 '+random.choice(nJ))
def shayari(m):bot.reply_to(m,random.choice(SH))
def fun(m):bot.reply_to(m,random.choice(['🎯 Kisi dost ko bina context “mission successful” bhej.','🧠 10 seconds mein 5 fruits ke naam bol.','🎭 Apni life ko ek movie title de.'])+'\n\n'+random.choice(nJ))
def profile(m):
    p=get_memory(m.from_user.id,m.from_user.first_name or 'Dost')['profile']; bot.reply_to(m,f'👤 Venu Profile\n\n📌 Name: {p.get("name")}\n🎮 Game: {p.get("favorite_game")}\n🎬 Movie: {p.get("favorite_movie")}\n🧠 Mood: {p.get("current_mood")}')
def mem(m):
    x=get_memory(m.from_user.id,m.from_user.first_name or 'Dost');p=x['profile'];bot.reply_to(m,f'🧠 Memory\n\nName: {p.get("name")}\nGame: {p.get("favorite_game")}\nHobbies: {p.get("hobbies")}\n\n💭 {x.get("summary")}')
def stats(m):
    uid=m.from_user.id; rows=db.request('GET',f'daily_stats?user_id=eq.{uid}&order=date.desc&limit=7') or []; tm=sum(int(x.get('messages_sent',0) or 0) for x in rows);tg=sum(int(x.get('games_played',0) or 0) for x in rows);bot.reply_to(m,f'📊 Stats\n\nMessages: {tm}\nGames: {tg}')
def help_(m):bot.reply_to(m,'ℹ️ Venu\n\n💬 Natural AI chat\n🎮 Guess / Truth-Dare / Riddle / Roast\n😂 Joke / Shayari / Fun\n🎙️ /voice /novoice\n🧠 /memory  👤 /profile  📊 /stats\n🧹 /clear  🆔 /id  🏓 /ping')
def add_group(m):
    if not BOT_USERNAME:return bot.reply_to(m,'Invite link abhi available nahi 😭')
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton('➕ Add Venu To Group',url=f'https://t.me/{BOT_USERNAME}?startgroup=true'));bot.reply_to(m,'Group select karo 😎',reply_markup=k)

# ================= GAMES =================
R=[('Tootne par awaaz nahi karti?','khamoshi'),('Jitna nikaalo utna bada hota hai?','gaddha'),('Keys hain, locks nahi; space hai, room nahi?','keyboard')];T=['Sabse embarrassing moment?','Weird talent kya hai?','Kis cheez se instantly khush hote ho?'];D=['Last emoji se funny sentence bana.','Kisi friend ko “mission successful 🫡” bhej.','Apni life ko movie title de.'];RO=['Teri typing dekh ke autocorrect bhi resign kar de 😂','Confidence 4K mein, logic 144p mein 😭','Tera plan solid tha... bas plan mein plan hi nahi tha 💀']
def start_game(m,t):
    uid=m.from_user.id;g={'type':t,'created':time.time(),'attempts':0};
    if t=='guess':g['secret']=random.randint(1,50);txt='🎯 Guess Number! 1–50 ke beech number bhej.'
    elif t=='tod':txt='🎲 Truth or Dare? `truth` ya `dare` bhej.'
    elif t=='riddle':g['question'],g['answer']=random.choice(R);txt='🧩 '+g['question']
    else:txt='🔥 Roast Battle! Koi line bhej, halka roast milega 😈'
    with lock:games[uid]=g
    bot.send_message(m.chat.id,txt,reply_markup=game_kb())
def game_process(m,text):
    uid=m.from_user.id
    with lock:g=games.get(uid)
    if not g:return False
    t=g['type'];x=text.strip().lower()
    if x in {'cancel','/cancel','exit','quit'}:
        with lock:games.pop(uid,None)
        bot.reply_to(m,'🎮 Game cancel.');return True
    if t=='guess':
        try:n=int(x)
        except:bot.reply_to(m,'🔢 Number bhej, jaise 27.');return True
        if not 1<=n<=50:bot.reply_to(m,'1 se 50 ke beech 😭');return True
        g['attempts']+=1
        if n==g['secret']:
            a=g['attempts'];s=g['secret'];games.pop(uid,None);bot.reply_to(m,f'🎉 Correct! {s} tha. Attempts: {a}')
        elif n<g['secret']:bot.reply_to(m,'📈 Thoda bada try kar.')
        else:bot.reply_to(m,'📉 Thoda chhota try kar.')
        return True
    if t=='tod':
        if x not in {'truth','dare'}:bot.reply_to(m,'Sirf truth ya dare 😎');return True
        with lock:games.pop(uid,None)
        bot.reply_to(m,('🧠 Truth: '+random.choice(T)) if x=='truth' else ('🔥 Dare: '+random.choice(D)));return True
    if t=='riddle':
        a=g['answer'];ok=x==a or a in x or SequenceMatcher(None,x,a).ratio()>=.72
        if ok:
            with lock:games.pop(uid,None)
            bot.reply_to(m,'🎉 Correct! Riddle master 🔥')
        else:bot.reply_to(m,'❌ Nope 😭 Ek aur try.')
        return True
    if t=='roast':
        with lock:games.pop(uid,None)
        bot.reply_to(m,random.choice(RO));return True
    return False

# ================= GROUP / NAME =================
def group_ok(m):
    if not RESPOND_IN_GROUPS:return False
    if m.chat.type not in {'group','supergroup'}:return True
    text=m.text or ''
    if text.startswith('/'):return True
    if BOT_USERNAME and f'@{BOT_USERNAME}' in text.lower():return True
    r=m.reply_to_message
    return bool(r and r.from_user and BOT_ID and r.from_user.id==BOT_ID)
def strip_mention(x):return re.sub(rf'@{re.escape(BOT_USERNAME)}\b','',x,flags=re.I).strip() if BOT_USERNAME else x.strip()
def name_prefix(uid,name):
    name=(name or '').strip(); now=time.time()
    if not name or len(name)>30 or not re.fullmatch(r'[\w .\'-]+',name,re.U):return ''
    with lock:last=name_time.get(uid,0)
    if now-last<600 or random.random()>.12:return ''
    with lock:name_time[uid]=now
    return name+', '

# ================= COMMANDS =================
@bot.message_handler(commands=['start'])
def start(m):
    register_user(m.from_user.id,m.from_user.username,m.from_user.first_name);bot.reply_to(m,'Oye bhai! ✨ Main Venu hoon. Kya scene hai? 😎',reply_markup=main_kb())
@bot.message_handler(commands=['help'])
def chelp(m):help_(m)
@bot.message_handler(commands=['profile'])
def cprofile(m):profile(m)
@bot.message_handler(commands=['memory'])
def cmem(m):mem(m)
@bot.message_handler(commands=['stats'])
def cstats(m):stats(m)
@bot.message_handler(commands=['clear'])
def cclear(m):clear_memory(m.from_user.id);bot.reply_to(m,'🧹 Memory clear. Fresh start 😌')
@bot.message_handler(commands=['voice'])
def voice(m):tts_users.add(m.from_user.id);bot.reply_to(m,'🎙️ Voice replies ON.')
@bot.message_handler(commands=['novoice'])
def novoice(m):tts_users.discard(m.from_user.id);bot.reply_to(m,'🔇 Voice replies OFF.')
@bot.message_handler(commands=['joke'])
def cjoke(m):joke(m)
@bot.message_handler(commands=['shayari'])
def cshayari(m):shayari(m)
@bot.message_handler(commands=['fun'])
def cfun(m):fun(m)
@bot.message_handler(commands=['dice'])
def cdice(m):bot.reply_to(m,f'🎲 {random.randint(1,6)}')
@bot.message_handler(commands=['coin'])
def ccoin(m):bot.reply_to(m,'🪙 '+random.choice(['Heads!','Tails!']))
@bot.message_handler(commands=['choose'])
def cchoose(m):
    a=[x.strip() for x in re.split(r'[,|]',m.text.partition(' ')[2]) if x.strip()];bot.reply_to(m,'🎯 '+random.choice(a) if len(a)>=2 else 'Usage: /choose chai, coffee')
@bot.message_handler(commands=['id'])
def cid(m):bot.reply_to(m,f'🆔 User: {m.from_user.id}\n💬 Chat: {m.chat.id}')
@bot.message_handler(commands=['ping'])
def ping(m):
    st=time.perf_counter();x=bot.reply_to(m,'🏓 Checking...');ms=round((time.perf_counter()-st)*1000,1)
    try:bot.edit_message_text(f'🏓 Pong! {ms} ms',m.chat.id,x.message_id)
    except:pass
@bot.message_handler(commands=['roast'])
def croast(m):start_game(m,'roast')

# ================= CALLBACKS =================
@bot.callback_query_handler(func=lambda c:True)
def callback(c):
    try:
        bot.answer_callback_query(c.id);m=c.message;d=c.data
        if d=='back':bot.edit_message_text('😎 Venu — kya karna hai?',m.chat.id,m.message_id,reply_markup=main_kb())
        elif d=='games':bot.edit_message_text('🎮 Game choose kar:',m.chat.id,m.message_id,reply_markup=game_kb())
        elif d in {'guess','tod','riddle','roast'}:start_game(m,{'guess':'guess','tod':'tod','riddle':'riddle','roast':'roast'}[d])
        elif d=='talk':bot.send_message(m.chat.id,'Bol bhai 😎')
        elif d=='memory':mem(m)
        elif d=='profile':profile(m)
        elif d=='fun':fun(m)
        elif d=='stats':stats(m)
        elif d=='voice':voice(m)
        elif d=='help':help_(m)
        elif d=='group':add_group(m)
        elif d=='clear':cclear(m)
    except Exception:logger.exception('callback error')

# ================= TEXT =================
def text_handler(m):
    typing=None
    try:
        if not group_ok(m):return
        uid=m.from_user.id; text=strip_mention(m.text or '').strip()
        if not text:return
        now=time.time()
        with lock:prev=last_msg.get(uid);last_msg[uid]=now;activity[uid]=now
        if prev and now-prev<.15:return
        register_user(uid,m.from_user.username,m.from_user.first_name)
        actions={'🎮 Guess Number':'guess','🔥 Roast Battle':'roast','🎯 Truth or Dare':'tod','🧩 Riddle Battle':'riddle'}
        if text in actions:start_game(m,actions[text]);return
        if text=='😂 Joke':joke(m);return
        if text=='❤️ Shayari':shayari(m);return
        if text=='🎲 Fun Zone':fun(m);return
        if text=='📊 My Stats':stats(m);return
        if text=='🧠 My Memory':mem(m);return
        if text in {'👤 My Profile','👤 View Profile'}:profile(m);return
        if text=='🎙️ Voice Mode':voice(m);return
        if text=='ℹ️ Help':help_(m);return
        if text=='➕ Add Me In Group':add_group(m);return
        if text=='🧹 Clear Chat':cclear(m);return
        if game_process(m,text):daily(uid,True);return
        r=calc(text)
        if r is not None:bot.reply_to(m,f'🧮 {r}');daily(uid);return
        typing=Typing(m.chat.id);typing.start()
        save_message(uid,'user',text)
        pkt=get_memory(uid,m.from_user.first_name or 'Dost')
        reply,md=ai(uid,pkt,text)
        pre=name_prefix(uid,m.from_user.first_name)
        if pre:reply=pre+reply
        reply=clean_reply(reply);update_profile(uid,'current_mood',md);save_message(uid,'assistant',reply);daily(uid)
        typing.close();typing=None
        bot.reply_to(m,reply)
        if uid in tts_users and ENABLE_TTS:threading.Thread(target=tts,args=(m.chat.id,reply),daemon=True).start()
    except Exception:
        logger.exception('text handler error')
        if typing:typing.close()
        try:bot.reply_to(m,'Bhai ek sec, connection hiccup hua 😭 phir se bol.')
        except:pass
bot.message_handler(content_types=['text'])(text_handler)

# ================= VOICE =================
def transcribe(m):
    try:
        f=bot.get_file(m.voice.file_id);data=bot.download_file(f.file_path)
        with tempfile.TemporaryDirectory() as d:
            o=os.path.join(d,'a.ogg');w=os.path.join(d,'a.wav');open(o,'wb').write(data);AudioSegment.from_file(o).export(w,format='wav');r=sr.Recognizer()
            with sr.AudioFile(w) as s:a=r.record(s)
            return r.recognize_google(a,language='hi-IN')
    except sr.UnknownValueError:return None
    except Exception:logger.exception('transcription error');return None
def tts(chat,text):
    try:
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,'v.mp3');gTTS(text=text,lang='hi').save(p)
            with open(p,'rb') as f:bot.send_voice(chat,f,caption='🎙️ Venu')
    except Exception:logger.exception('TTS error')
@bot.message_handler(content_types=['voice'])
def voice_handler(m):
    ty=Typing(m.chat.id)
    try:
        if not group_ok(m):return
        ty.start();uid=m.from_user.id;register_user(uid,m.from_user.username,m.from_user.first_name);text=transcribe(m)
        if not text:ty.close();bot.reply_to(m,'🎙️ Awaaz clear nahi aayi 😭');return
        save_message(uid,'user','[Voice] '+text);pkt=get_memory(uid,m.from_user.first_name or 'Dost');reply,md=ai(uid,pkt,text);reply=clean_reply(reply);update_profile(uid,'current_mood',md);save_message(uid,'assistant',reply);daily(uid);ty.close();bot.reply_to(m,'🎙️ '+reply)
        if uid in tts_users and ENABLE_TTS:threading.Thread(target=tts,args=(m.chat.id,reply),daemon=True).start()
    except Exception:logger.exception('voice handler error');ty.close()

# ================= ADMIN =================
def is_admin(m):return bool(ADMIN_ID and m.from_user and m.from_user.id==ADMIN_ID)
@bot.message_handler(commands=['refresh'])
def refresh(m):
    if not is_admin(m):return bot.reply_to(m,'⛔ Admin only.')
    with lock:memory.clear();registered.clear();recent_replies.clear();games.clear();last_msg.clear();name_time.clear();activity.clear()
    bot.reply_to(m,'♻️ State refreshed.')
@bot.message_handler(commands=['broadcast'])
def broadcast(m):
    if not is_admin(m):return bot.reply_to(m,'⛔ Admin only.')
    text=m.text.partition(' ')[2].strip(); rows=db.request('GET','users?select=user_id',timeout=10) or []
    if not text:return bot.reply_to(m,'Usage: /broadcast your message')
    bot.reply_to(m,f'📢 Sending to {len(rows)} users...')
    def w():
        ok=bad=0
        for row in rows:
            try:bot.send_message(int(row['user_id']),text);ok+=1;time.sleep(.05)
            except:bad+=1
        try:bot.send_message(m.chat.id,f'📢 Done\n✅ {ok}\n❌ {bad}')
        except:pass
    threading.Thread(target=w,daemon=True).start()

# ================= CLEANUP / MAIN =================
def cleanup():
    while True:
        time.sleep(300)
        try:
            now=time.time()
            with lock:
                for uid,g in list(games.items()):
                    if now-g.get('created',now)>1800:games.pop(uid,None)
                for uid,t in list(activity.items()):
                    if now-t>7200:activity.pop(uid,None);last_msg.pop(uid,None)
        except Exception:logger.exception('cleanup error')
def main():
    logger.info('🚀 Venu production bot starting | AI=%s | model=%s | timeout=%s',AI_BASE_URL,AI_MODEL,AI_TIMEOUT)
    threading.Thread(target=run_flask,daemon=True).start();threading.Thread(target=cleanup,daemon=True).start()
    try:bot.remove_webhook()
    except:logger.exception('remove webhook failed')
    while True:
        try:
            bot.infinity_polling(timeout=25,long_polling_timeout=25,skip_pending=True,allowed_updates=['message','callback_query'])
        except KeyboardInterrupt:break
        except Exception:logger.exception('Polling crashed; reconnecting');time.sleep(3)
if __name__=='__main__':main()
