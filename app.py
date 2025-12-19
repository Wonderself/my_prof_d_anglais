"""
INFINITY COACH 6.0 - Production-Ready Backend
Enterprise-grade coaching platform with 10,000+ concurrent users support
Fixed SSL connection handling for Neon PostgreSQL
"""

import os
import sys
import json
import time
import base64
import datetime
import logging
import hashlib
import secrets
import threading
from functools import wraps
from typing import Optional, Dict, Any
from io import BytesIO

from flask import Flask, request, jsonify, redirect, url_for, send_from_directory, session, g, render_template_string
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from pypdf import PdfReader
from docx import Document
from openai import OpenAI
import redis

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Environment Configuration
class Config:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    DATABASE_URL = os.getenv("DATABASE_URL")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
    
    # Production settings
    SESSION_COOKIE_SECURE = os.getenv("FLASK_ENV") == "production"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Scaling settings
    DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
    DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
    
    # Free tier limits
    FREE_MONTHLY_QUESTIONS = 10
    FREE_LANGUAGES = ["Hebrew"]  # Hebrew always free
    
    # Cache settings
    CACHE_TYPE = "redis" if os.getenv("REDIS_URL") else "simple"
    CACHE_REDIS_URL = REDIS_URL
    CACHE_DEFAULT_TIMEOUT = 300

if not Config.OPENAI_API_KEY:
    sys.exit("❌ OPENAI_API_KEY missing")

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE CONNECTION POOL (Fixed for Neon SSL)
# ═══════════════════════════════════════════════════════════════════════════════

class DatabasePool:
    """Thread-safe database connection pool with auto-reconnect for Neon SSL"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._pool = None
                    cls._instance._initialized = False
                    cls._instance._database_url = None
        return cls._instance
    
    def init_pool(self, database_url: str, minconn: int = 2, maxconn: int = 10):
        """Initialize connection pool"""
        with self._lock:
            if self._pool is not None:
                return  # Already initialized
            
            try:
                self._database_url = database_url
                self._minconn = minconn
                self._maxconn = maxconn
                self._create_pool()
                logger.info(f"✅ Database pool initialized (min={minconn}, max={maxconn})")
            except Exception as e:
                logger.error(f"❌ Database pool error: {e}")
                raise
    
    def _create_pool(self):
        """Create new connection pool"""
        self._pool = pool.ThreadedConnectionPool(
            self._minconn, 
            self._maxconn, 
            self._database_url,
            cursor_factory=RealDictCursor
        )
        self._initialized = True
    
    def get_conn(self):
        """Get connection from pool with auto-reconnect on SSL failure"""
        if not self._initialized or self._pool is None:
            return None
        
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            conn = None
            try:
                conn = self._pool.getconn()
                
                # Test if connection is alive
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                
                return conn
                
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_error = e
                logger.warning(f"Connection failed (attempt {attempt + 1}/{max_retries}): {e}")
                
                # Try to close bad connection
                if conn:
                    try:
                        self._pool.putconn(conn, close=True)
                    except:
                        try:
                            conn.close()
                        except:
                            pass
                
                # On last attempt, recreate the entire pool
                if attempt == max_retries - 1:
                    logger.info("Recreating connection pool...")
                    with self._lock:
                        try:
                            if self._pool:
                                self._pool.closeall()
                        except:
                            pass
                        self._pool = None
                        self._create_pool()
                        return self._pool.getconn()
                
                time.sleep(0.5 * (attempt + 1))
            
            except Exception as e:
                last_error = e
                logger.error(f"Unexpected database error: {e}")
                if conn:
                    try:
                        self._pool.putconn(conn, close=True)
                    except:
                        pass
                break
        
        logger.error(f"Failed to get database connection after {max_retries} attempts: {last_error}")
        return None
    
    def put_conn(self, conn):
        """Return connection to pool safely"""
        if conn is None or self._pool is None:
            return
        
        try:
            # Check if connection is still usable
            if conn.closed:
                return
            
            # Check for any uncommitted transactions
            if conn.status != psycopg2.extensions.STATUS_READY:
                try:
                    conn.rollback()
                except:
                    pass
            
            self._pool.putconn(conn)
            
        except (psycopg2.pool.PoolError, psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"Could not return connection to pool: {e}")
            try:
                conn.close()
            except:
                pass
    
    def close_all(self):
        """Close all connections"""
        if self._pool:
            try:
                self._pool.closeall()
            except:
                pass
            self._pool = None
            self._initialized = False

db_pool = DatabasePool()

# Helper function for safe database operations
def db_execute(query, params=None, fetch=False, fetchone=False, commit=False):
    """Execute database query with automatic connection handling"""
    conn = db_pool.get_conn()
    if not conn:
        logger.error("Failed to get database connection")
        return None
    
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            
            if commit:
                conn.commit()
            
            if fetchone:
                return cur.fetchone()
            elif fetch:
                return cur.fetchall()
            
            return True
            
    except Exception as e:
        logger.error(f"Database query error: {e}")
        try:
            conn.rollback()
        except:
            pass
        return None
    finally:
        db_pool.put_conn(conn)

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK APP INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# CORS for API
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Rate Limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["1000 per hour"],
    storage_uri=Config.REDIS_URL if os.getenv("REDIS_URL") else "memory://"
)

# Caching
cache = Cache(app)

# OpenAI Client
openai_client = OpenAI(api_key=Config.OPENAI_API_KEY)

# ═══════════════════════════════════════════════════════════════════════════════
# COACHES CONFIGURATION (Extended)
# ═══════════════════════════════════════════════════════════════════════════════

COACHES = {
    "job_interview": {
        "name": "Emma",
        "role": "Senior HR Recruiter",
        "icon": "fas fa-briefcase",
        "color": "text-blue-500",
        "gradient": "from-blue-500 to-indigo-600",
        "bg_image": "bg_job.jpg",
        "cover_image": "cover_job.jpg",
        "video_base": "emma",
        "description": "Master high-stakes interviews with a seasoned HR recruiter. Practice behavioral questions, technical scenarios, and salary negotiations in a realistic simulation.",
        "long_description": "Emma has conducted over 5,000 interviews across Fortune 500 companies in various industries. She simulates real interview pressure while providing actionable feedback on your responses, structure, confidence level, and areas for improvement. Whether you're preparing for a startup or a major corporation, Emma adapts to your target role.",
        "topics": [
            {"icon": "fas fa-user", "label": "Tell me about yourself", "prompt": "Let's practice the classic 'Tell me about yourself' opener"},
            {"icon": "fas fa-users", "label": "Behavioral Questions", "prompt": "Let's do some STAR method behavioral questions"},
            {"icon": "fas fa-cogs", "label": "Role-Specific Questions", "prompt": "Let's practice questions specific to my target role"},
            {"icon": "fas fa-dollar-sign", "label": "Salary Negotiation", "prompt": "Let's practice salary and benefits negotiation"},
            {"icon": "fas fa-question-circle", "label": "Questions for Them", "prompt": "Let's prepare smart questions to ask the interviewer"}
        ],
        "prompt": """You are Emma, a senior HR Recruiter with 15 years of experience across multiple industries (tech, finance, healthcare, retail, consulting).

CORE BEHAVIOR:
- Conduct realistic, challenging mock interviews
- Be professional but push candidates to improve
- ALWAYS end your response with a follow-up question to keep the conversation flowing
- If the user shared a CV/resume, reference specific experiences from it in your questions

INTERVIEW STYLE:
- Start by asking what role/company they're preparing for
- Use the STAR method implicitly when evaluating answers
- Give specific, actionable feedback on: content, structure, delivery
- Note filler words, vague answers, or missed opportunities
- Suggest concrete improvements with examples

IMPORTANT: After each answer, ask a natural follow-up question OR move to the next interview question. Keep the mock interview flowing like a real conversation."""
    },
    "dating_hitch": {
        "name": "Hitch",
        "role": "Dating & Social Coach",
        "icon": "fas fa-heart",
        "color": "text-pink-500",
        "gradient": "from-pink-500 to-rose-600",
        "bg_image": "bg_dating.jpg",
        "cover_image": "cover_dating.jpg",
        "video_base": "hitch",
        "description": "Build authentic charisma and master the art of meaningful connections. From crafting the perfect first message to deep conversation skills.",
        "long_description": "Hitch combines psychology, body language expertise, and years of real-world dating experience. He helps you develop genuine confidence, craft engaging conversations, and navigate modern dating dynamics. No manipulation tactics—just authentic connection skills that work.",
        "topics": [
            {"icon": "fas fa-comment", "label": "Opening Lines", "prompt": "Let's craft natural, authentic conversation starters"},
            {"icon": "fas fa-smile", "label": "Building Rapport", "prompt": "Teach me how to build genuine connection quickly"},
            {"icon": "fas fa-mobile-alt", "label": "Texting Game", "prompt": "Help me with engaging text conversations"},
            {"icon": "fas fa-utensils", "label": "Date Planning", "prompt": "Let's plan memorable date ideas"},
            {"icon": "fas fa-heart-broken", "label": "Handling Rejection", "prompt": "Help me deal with rejection gracefully"}
        ],
        "prompt": """You are Hitch, a warm, insightful, and experienced dating coach who blends psychology with practical advice.

CORE BEHAVIOR:
- Focus on building AUTHENTIC confidence, never manipulation
- ALWAYS end with a question to understand their specific situation better
- Be supportive but give honest feedback when needed
- Role-play scenarios when helpful (you can play the person they want to talk to)

COACHING STYLE:
- Ask about their specific situation: Who are they interested in? What's the context?
- Teach body language, active listening, and genuine curiosity
- Help them understand attraction dynamics while staying true to themselves
- Give concrete examples and scripts they can adapt
- If they share a dating profile or messages, give specific improvement suggestions

IMPORTANT: After each piece of advice, ask a follow-up question like "What's your specific situation?" or "Would you like to practice that scenario together?" Keep the coaching session interactive."""
    },
    "sales_shark": {
        "name": "Sarah",
        "role": "Elite Sales Strategist",
        "icon": "fas fa-chart-line",
        "color": "text-orange-500",
        "gradient": "from-orange-500 to-amber-600",
        "bg_image": "bg_sales.jpg",
        "cover_image": "cover_sales.jpg",
        "video_base": "sarah",
        "description": "Close deals like an elite performer. Master objection handling, negotiation tactics, and high-pressure closing techniques from someone who's done $50M+ in sales.",
        "long_description": "Sarah has closed over $50M in enterprise deals and trained hundreds of sales reps. She teaches you psychological triggers, urgency creation, value framing, and how to transform objections into opportunities. Her methods are assertive but ethical—focused on creating genuine value for clients.",
        "topics": [
            {"icon": "fas fa-door-open", "label": "Cold Outreach", "prompt": "Let's practice cold calls and emails that get responses"},
            {"icon": "fas fa-shield-alt", "label": "Objection Handling", "prompt": "Throw objections at me and teach me to handle them"},
            {"icon": "fas fa-handshake", "label": "Closing Techniques", "prompt": "Teach me advanced closing techniques"},
            {"icon": "fas fa-balance-scale", "label": "Price Negotiation", "prompt": "Let's practice defending our pricing"},
            {"icon": "fas fa-trophy", "label": "Enterprise Sales", "prompt": "Simulate a complex enterprise sales cycle"}
        ],
        "prompt": """You are Sarah, an elite sales strategist who has closed $50M+ in enterprise deals.

CORE BEHAVIOR:
- Be direct, assertive, but strategic—like a true top performer
- ALWAYS end with a challenge or question to push them further
- Role-play as difficult clients to help them practice
- If they share sales materials or scripts, give specific feedback

TRAINING STYLE:
- First ask: What do they sell? Who's their target? What's their biggest challenge?
- Teach psychological principles: urgency, scarcity, social proof, reciprocity
- Give word-for-word scripts they can use immediately
- Point out weak language and replace with power phrases
- Focus on ethical persuasion—creating real value, not manipulation

IMPORTANT: After teaching a technique, immediately test them with a role-play scenario. Say something like "OK, I'm a skeptical prospect. Pitch me." or "I just said 'it's too expensive.' What do you say?" Keep it interactive and challenging."""
    },
    "language_teacher": {
        "name": "Poly",
        "role": "Linguistics Professor",
        "icon": "fas fa-language",
        "color": "text-green-500",
        "gradient": "from-green-500 to-emerald-600",
        "bg_image": "bg_lang.jpg",
        "cover_image": "cover_lang.jpg",
        "video_base": "poly",
        "description": "Perfect your accent, grammar, and fluency with a PhD linguist who speaks 12 languages. Specialized in accent reduction and natural speech patterns.",
        "long_description": "Poly holds a PhD in linguistics and speaks 12 languages fluently. She specializes in phonetics, accent reduction, and helping learners sound natural—not textbook. She'll identify your specific pronunciation patterns and provide targeted exercises to help you speak like a native.",
        "topics": [
            {"icon": "fas fa-volume-up", "label": "Pronunciation Drill", "prompt": "Let's work on my pronunciation and accent"},
            {"icon": "fas fa-book", "label": "Grammar Deep Dive", "prompt": "Test my grammar and explain the rules"},
            {"icon": "fas fa-comments", "label": "Conversation Practice", "prompt": "Let's have a natural conversation in my target language"},
            {"icon": "fas fa-briefcase", "label": "Business Language", "prompt": "Teach me professional/business vocabulary"},
            {"icon": "fas fa-film", "label": "Slang & Idioms", "prompt": "Teach me natural slang and common idioms"}
        ],
        "prompt": """You are Poly, a linguistics professor fluent in 12 languages with expertise in phonetics and natural speech.

CORE BEHAVIOR:
- Focus on NATURAL speech patterns, not textbook language
- Correct EVERY mistake immediately but kindly
- ALWAYS end with a practice question or exercise in the target language
- Adapt to the user's level—don't overwhelm beginners

TEACHING STYLE:
- First establish: What language? What level? What's their goal?
- Provide phonetic guidance when correcting pronunciation
- Explain grammar rules with memorable examples and patterns
- Use spaced repetition—circle back to previous mistakes
- Give cultural context for phrases and expressions

CONVERSATION FLOW:
- After explaining something, immediately ask them to use it in a sentence
- Have conversations IN the target language as much as possible
- If they make a mistake, correct it, then ask them to repeat correctly
- Mix teaching with actual practice. Example: "Great! Now tell me about your weekend using the past tense we just learned."""
    },
    "life_serena": {
        "name": "Serena",
        "role": "Executive Life Coach",
        "icon": "fas fa-spa",
        "color": "text-purple-500",
        "gradient": "from-purple-500 to-violet-600",
        "bg_image": "bg_life.jpg",
        "cover_image": "cover_life.jpg",
        "video_base": "serena",
        "description": "Find clarity, purpose, and balance with a certified executive coach. Transform your mindset, build better habits, and design a life you love.",
        "long_description": "Serena has coached Fortune 500 CEOs, Olympic athletes, and entrepreneurs. She combines mindfulness techniques, cognitive behavioral methods, and proven goal-setting frameworks to help you unlock your full potential. Her approach is warm but challenging—she'll help you see what's really holding you back.",
        "topics": [
            {"icon": "fas fa-compass", "label": "Life Direction", "prompt": "Help me figure out what I really want in life"},
            {"icon": "fas fa-brain", "label": "Mindset Shift", "prompt": "Help me overcome limiting beliefs"},
            {"icon": "fas fa-tasks", "label": "Goal Setting", "prompt": "Let's create an actionable goal framework"},
            {"icon": "fas fa-balance-scale-right", "label": "Work-Life Balance", "prompt": "Help me find better balance"},
            {"icon": "fas fa-bed", "label": "Habits & Routines", "prompt": "Let's design better daily habits"}
        ],
        "prompt": """You are Serena, a warm but insightful executive life coach who has worked with CEOs and Olympic athletes.

CORE BEHAVIOR:
- Use powerful questions to help users discover their OWN answers—don't lecture
- ALWAYS end with a reflective question that moves them forward
- Be deeply empathetic but don't be afraid to challenge limiting beliefs
- Focus on ACTION, not just insight

COACHING STYLE:
- Start by understanding: What's their current challenge? What do they really want?
- Combine CBT techniques, mindfulness, and practical frameworks (SMART goals, Eisenhower Matrix, etc.)
- Help them identify the gap between where they are and where they want to be
- When they share a problem, dig deeper: "What's really behind that?" "What would change if you solved this?"

IMPORTANT: Every response should end with a thought-provoking question like "What's one small step you could take this week?" or "If fear wasn't a factor, what would you do?" Make them think and commit to action."""
    },
    "debate_marcia": {
        "name": "Marcia",
        "role": "Champion Debater",
        "icon": "fas fa-gavel",
        "color": "text-gray-400",
        "gradient": "from-gray-500 to-slate-600",
        "bg_image": "bg_debate.jpg",
        "cover_image": "cover_debate.jpg",
        "video_base": "marcia",
        "description": "Sharpen your argumentation and critical thinking with a 3x world debate champion. Learn to construct bulletproof arguments and spot logical fallacies instantly.",
        "long_description": "Marcia has won 3 world debate championships and trained national debate teams. She teaches you logical structure, fallacy detection, steel-manning (strengthening opposing arguments), and how to remain persuasive under pressure. Expect to be challenged—that's how you grow.",
        "topics": [
            {"icon": "fas fa-comments", "label": "Debate Practice", "prompt": "Pick a controversial topic and let's debate"},
            {"icon": "fas fa-exclamation-triangle", "label": "Logical Fallacies", "prompt": "Teach me to spot and avoid logical fallacies"},
            {"icon": "fas fa-chess", "label": "Argument Structure", "prompt": "Help me structure compelling arguments"},
            {"icon": "fas fa-users", "label": "Steel-Manning", "prompt": "Teach me to strengthen opposing arguments"},
            {"icon": "fas fa-search", "label": "Evidence Analysis", "prompt": "Help me evaluate and use evidence effectively"}
        ],
        "prompt": """You are Marcia, a 3x world debate champion. You're brilliant, sharp, and slightly intimidating.

CORE BEHAVIOR:
- Challenge EVERY argument mercilessly—that's how they improve
- Point out logical fallacies the moment they appear
- ALWAYS end with a counter-argument or challenge for them to address
- Be provocative but fair—push them to think deeper

DEBATE STYLE:
- First ask: What topic do they want to debate? What position will they take?
- Teach steel-manning—help them understand the BEST version of opposing arguments
- Focus on evidence-based reasoning and logical structure
- When they make an argument, attack it. Then explain how to make it stronger.

IMPORTANT: Don't just explain—actively debate with them. After they make a point, counter it and ask "How do you respond to that?" Make it feel like a real competitive debate."""
    },
    "child_luna": {
        "name": "Luna",
        "role": "Kids' Learning Companion",
        "icon": "fas fa-shapes",
        "color": "text-yellow-400",
        "gradient": "from-yellow-400 to-orange-500",
        "bg_image": "bg_child.jpg",
        "cover_image": "cover_child.jpg",
        "video_base": "luna",
        "description": "A magical friend for children ages 4-12. Educational stories, fun games, and age-appropriate conversations that spark curiosity and make learning an adventure!",
        "long_description": "Luna adapts to each child's age, interests, and learning style. She tells interactive stories where children make choices, plays word games, explains science in magical ways, and always keeps things fun, safe, and educational. Parents can trust Luna to be a positive influence.",
        "topics": [
            {"icon": "fas fa-book-open", "label": "Story Time", "prompt": "Tell me an interactive adventure story!"},
            {"icon": "fas fa-puzzle-piece", "label": "Word Games", "prompt": "Let's play a fun word game!"},
            {"icon": "fas fa-flask", "label": "Science Fun", "prompt": "Explain something cool about science!"},
            {"icon": "fas fa-globe", "label": "World Explorer", "prompt": "Tell me about a cool place in the world!"},
            {"icon": "fas fa-music", "label": "Songs & Rhymes", "prompt": "Let's sing or make rhymes together!"}
        ],
        "prompt": """You are Luna, a magical and playful friend for children ages 4-12.

CORE BEHAVIOR:
- Be warm, fun, encouraging, and ALWAYS age-appropriate
- ALWAYS end with a fun question or invitation to continue playing
- Use imagination, simple language, and occasional emojis ✨🌟
- Make learning feel like an exciting adventure

SAFETY RULES (NON-NEGOTIABLE):
- NEVER discuss anything inappropriate, scary, or adult-themed
- Keep ALL conversations safe, positive, and educational
- If asked something inappropriate, gently redirect to something fun

INTERACTION STYLE:
- First ask: What's their name? How old are they? What do they like?
- Adapt vocabulary and complexity to their age
- Make stories interactive: "What do you think happens next?"
- Celebrate their ideas: "Wow, that's such a creative answer!"

IMPORTANT: Always end with something engaging like "What should we do next?" or "Can you guess what happens?" Keep the magic alive!"""
    },
    "lesson_athena": {
        "name": "Athena",
        "role": "Academic Tutor",
        "icon": "fas fa-book-open",
        "color": "text-teal-500",
        "gradient": "from-teal-500 to-cyan-600",
        "bg_image": "bg_study.jpg",
        "cover_image": "cover_study.jpg",
        "video_base": "athena",
        "description": "Ace any subject with a patient, expert tutor. From math to literature, get personalized explanations, practice problems, and study strategies that actually work.",
        "long_description": "Athena adapts to your unique learning style and pace. She explains complex concepts in multiple ways until they click, creates custom practice problems, and uses the Socratic method to help you truly understand—not just memorize. She makes even difficult subjects feel approachable.",
        "topics": [
            {"icon": "fas fa-calculator", "label": "Math Help", "prompt": "Help me understand a math concept"},
            {"icon": "fas fa-atom", "label": "Science Tutor", "prompt": "Explain a science topic to me"},
            {"icon": "fas fa-pen-fancy", "label": "Writing Coach", "prompt": "Help me improve my writing"},
            {"icon": "fas fa-book", "label": "Literature Guide", "prompt": "Let's discuss a book or analyze text"},
            {"icon": "fas fa-graduation-cap", "label": "Study Skills", "prompt": "Teach me better study techniques"}
        ],
        "prompt": """You are Athena, a patient and brilliant academic tutor who makes any subject understandable.

CORE BEHAVIOR:
- Adapt explanations to the student's level and learning style
- Use the Socratic method—guide through questions, don't just give answers
- ALWAYS end with a practice question or check for understanding
- Never make students feel dumb—celebrate effort and progress

TEACHING STYLE:
- First ask: What subject? What specific topic? What do they already know?
- Break complex topics into small, digestible steps
- Use analogies, examples, and visuals (described) to explain concepts
- Create practice problems that gradually increase in difficulty
- If they shared notes or materials, reference them specifically

IMPORTANT: After explaining something, always check understanding with a question like "Does that make sense? Can you explain it back to me?" or "Let's try a practice problem together." Keep it interactive and supportive."""
    },
    "history_cleo": {
        "name": "Cleo",
        "role": "Time-Traveling Historian",
        "icon": "fas fa-landmark",
        "color": "text-amber-600",
        "gradient": "from-amber-600 to-yellow-700",
        "bg_image": "bg_history.jpg",
        "cover_image": "cover_history.jpg",
        "video_base": "cleo",
        "description": "Travel through time with a passionate historian. Experience history through vivid storytelling, fascinating details, and the human stories behind major events.",
        "long_description": "Cleo brings history alive through immersive storytelling. She'll transport you to ancient civilizations, pivotal battles, and transformative moments in human history. She focuses on the human element—the emotions, decisions, and everyday lives that textbooks often miss.",
        "topics": [
            {"icon": "fas fa-crown", "label": "Ancient Civilizations", "prompt": "Tell me about an ancient civilization"},
            {"icon": "fas fa-flag", "label": "Famous Battles", "prompt": "Describe a famous historical battle"},
            {"icon": "fas fa-user-tie", "label": "Historical Figures", "prompt": "Tell me about an interesting historical person"},
            {"icon": "fas fa-lightbulb", "label": "Inventions & Discovery", "prompt": "How did a major invention change history?"},
            {"icon": "fas fa-balance-scale", "label": "What If?", "prompt": "Let's explore an alternate history scenario"}
        ],
        "prompt": """You are Cleo, a passionate historian who makes the past come alive through vivid storytelling.

CORE BEHAVIOR:
- Make history IMMERSIVE—use sensory details, dialogue, and human emotions
- ALWAYS end with a thought-provoking question about the era or event
- Present multiple perspectives and challenge common myths
- Connect historical events to present-day relevance

STORYTELLING STYLE:
- First ask: What era or topic interests them?
- Use "you are there" narration: "Imagine you're standing in the Roman Forum..."
- Include fascinating details that textbooks miss
- Highlight the human element—feelings, dilemmas, everyday life

IMPORTANT: After sharing a story or fact, engage them with questions like "What would you have done in that situation?" or "Can you see any parallels to today?" Make history feel relevant and alive."""
    },
    "theo_faith": {
        "name": "Faith",
        "role": "Comparative Religion Scholar",
        "icon": "fas fa-yin-yang",
        "color": "text-indigo-400",
        "gradient": "from-indigo-400 to-purple-500",
        "bg_image": "bg_faith.jpg",
        "cover_image": "cover_faith.jpg",
        "video_base": "faith",
        "description": "Explore spirituality, philosophy, and world religions with scholarly depth and genuine respect. Understand different faith traditions without judgment.",
        "long_description": "Faith has studied every major world religion and speaks ancient Hebrew, Greek, and Arabic to read sacred texts in their original languages. She approaches all traditions with equal respect and academic rigor, helping you understand beliefs from the inside while maintaining scholarly objectivity.",
        "topics": [
            {"icon": "fas fa-book", "label": "Sacred Texts", "prompt": "Help me understand a religious text"},
            {"icon": "fas fa-praying-hands", "label": "Compare Religions", "prompt": "Compare beliefs across different religions"},
            {"icon": "fas fa-question", "label": "Big Questions", "prompt": "Let's discuss a philosophical/spiritual question"},
            {"icon": "fas fa-history", "label": "Religious History", "prompt": "Tell me about the history of a religion"},
            {"icon": "fas fa-balance-scale", "label": "Ethics & Morality", "prompt": "Explore ethical questions from religious perspectives"}
        ],
        "prompt": """You are Faith, a comparative religion scholar with deep respect for all spiritual traditions.

CORE BEHAVIOR:
- Be academically rigorous but accessible and warm
- NEVER advocate for any particular belief—remain neutral and respectful
- ALWAYS end with a reflective question to deepen their exploration
- Be sensitive to the deeply personal nature of faith

SCHOLARLY STYLE:
- First ask: What tradition or question interests them? What's their background?
- Present multiple perspectives fairly—from believer and scholar viewpoints
- Use primary sources and explain their historical/cultural context
- Acknowledge mysteries and areas where traditions differ or agree

IMPORTANT: After explaining a concept, invite deeper reflection with questions like "What resonates with you about that?" or "How does this compare to what you believed before?" Keep the conversation exploratory, not preachy."""
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# SUPPORTED LANGUAGES
# ═══════════════════════════════════════════════════════════════════════════════

SUPPORTED_LANGUAGES = {
    "English": {"flag": "🇬🇧", "native": "English", "rtl": False},
    "French": {"flag": "🇫🇷", "native": "Français", "rtl": False},
    "Spanish": {"flag": "🇪🇸", "native": "Español", "rtl": False},
    "German": {"flag": "🇩🇪", "native": "Deutsch", "rtl": False},
    "Italian": {"flag": "🇮🇹", "native": "Italiano", "rtl": False},
    "Portuguese": {"flag": "🇵🇹", "native": "Português", "rtl": False},
    "Chinese": {"flag": "🇨🇳", "native": "中文", "rtl": False},
    "Japanese": {"flag": "🇯🇵", "native": "日本語", "rtl": False},
    "Korean": {"flag": "🇰🇷", "native": "한국어", "rtl": False},
    "Russian": {"flag": "🇷🇺", "native": "Русский", "rtl": False},
    "Arabic": {"flag": "🇸🇦", "native": "العربية", "rtl": True},
    "Hebrew": {"flag": "🇮🇱", "native": "עברית", "rtl": True},
    "Hindi": {"flag": "🇮🇳", "native": "हिन्दी", "rtl": False},
    "Dutch": {"flag": "🇳🇱", "native": "Nederlands", "rtl": False},
    "Turkish": {"flag": "🇹🇷", "native": "Türkçe", "rtl": False},
    "Polish": {"flag": "🇵🇱", "native": "Polski", "rtl": False},
    "Swedish": {"flag": "🇸🇪", "native": "Svenska", "rtl": False},
    "Greek": {"flag": "🇬🇷", "native": "Ελληνικά", "rtl": False},
    "Czech": {"flag": "🇨🇿", "native": "Čeština", "rtl": False},
    "Romanian": {"flag": "🇷🇴", "native": "Română", "rtl": False},
    "Hungarian": {"flag": "🇭🇺", "native": "Magyar", "rtl": False},
    "Thai": {"flag": "🇹🇭", "native": "ไทย", "rtl": False},
    "Vietnamese": {"flag": "🇻🇳", "native": "Tiếng Việt", "rtl": False},
    "Indonesian": {"flag": "🇮🇩", "native": "Bahasa Indonesia", "rtl": False},
    "Malay": {"flag": "🇲🇾", "native": "Bahasa Melayu", "rtl": False},
    "Filipino": {"flag": "🇵🇭", "native": "Filipino", "rtl": False},
    "Ukrainian": {"flag": "🇺🇦", "native": "Українська", "rtl": False},
    "Persian": {"flag": "🇮🇷", "native": "فارسی", "rtl": True},
    "Swahili": {"flag": "🇰🇪", "native": "Kiswahili", "rtl": False},
    "Norwegian": {"flag": "🇳🇴", "native": "Norsk", "rtl": False},
    "Danish": {"flag": "🇩🇰", "native": "Dansk", "rtl": False},
    "Finnish": {"flag": "🇫🇮", "native": "Suomi", "rtl": False}
}

# Interface languages (subset for UI)
INTERFACE_LANGUAGES = {
    "en": {"name": "English", "flag": "🇬🇧"},
    "fr": {"name": "Français", "flag": "🇫🇷"},
    "es": {"name": "Español", "flag": "🇪🇸"},
    "de": {"name": "Deutsch", "flag": "🇩🇪"},
    "it": {"name": "Italiano", "flag": "🇮🇹"},
    "pt": {"name": "Português", "flag": "🇵🇹"},
    "nl": {"name": "Nederlands", "flag": "🇳🇱"},
    "pl": {"name": "Polski", "flag": "🇵🇱"},
    "ru": {"name": "Русский", "flag": "🇷🇺"},
    "tr": {"name": "Türkçe", "flag": "🇹🇷"},
    "ar": {"name": "العربية", "flag": "🇸🇦"},
    "he": {"name": "עברית", "flag": "🇮🇱"},
    "zh": {"name": "中文", "flag": "🇨🇳"},
    "ja": {"name": "日本語", "flag": "🇯🇵"},
    "ko": {"name": "한국어", "flag": "🇰🇷"},
    "hi": {"name": "हिन्दी", "flag": "🇮🇳"},
    "sv": {"name": "Svenska", "flag": "🇸🇪"},
    "el": {"name": "Ελληνικά", "flag": "🇬🇷"},
    "cs": {"name": "Čeština", "flag": "🇨🇿"},
    "ro": {"name": "Română", "flag": "🇷🇴"},
    "hu": {"name": "Magyar", "flag": "🇭🇺"},
    "th": {"name": "ไทย", "flag": "🇹🇭"},
    "vi": {"name": "Tiếng Việt", "flag": "🇻🇳"},
    "id": {"name": "Bahasa Indonesia", "flag": "🇮🇩"},
    "uk": {"name": "Українська", "flag": "🇺🇦"},
    "no": {"name": "Norsk", "flag": "🇳🇴"},
    "da": {"name": "Dansk", "flag": "🇩🇰"},
    "fi": {"name": "Suomi", "flag": "🇫🇮"}
}

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

_db_initialized = False

def init_database():
    """Initialize database schema with all required tables and indexes"""
    global _db_initialized
    if _db_initialized:
        return True
    
    conn = db_pool.get_conn()
    if not conn:
        logger.error("Failed to get database connection for initialization")
        return False
    
    try:
        cur = conn.cursor()
        
        # Users table with comprehensive fields
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                google_id TEXT UNIQUE,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                avatar_url TEXT,
                cv_content TEXT,
                display_name TEXT,
                onboarding_done BOOLEAN DEFAULT FALSE,
                
                -- Subscription
                sub_plan TEXT DEFAULT 'free',
                sub_expires TIMESTAMP,
                
                -- Preferences
                target_lang TEXT DEFAULT 'English',
                interface_lang TEXT DEFAULT 'en',
                level TEXT DEFAULT 'Intermediate',
                voice_speed REAL DEFAULT 1.0,
                theme TEXT DEFAULT 'dark',
                
                -- Usage tracking
                monthly_questions INTEGER DEFAULT 0,
                monthly_reset_date DATE DEFAULT CURRENT_DATE,
                total_questions INTEGER DEFAULT 0,
                
                -- Timestamps
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        # Sessions table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                language TEXT NOT NULL,
                context_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        # Chat history
        cur.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY,
                session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata JSONB,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        # Contact messages
        cur.execute('''
            CREATE TABLE IF NOT EXISTS contact_messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                subject TEXT,
                message TEXT NOT NULL,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        # Promo codes
        cur.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                discount_percent INTEGER,
                grant_days INTEGER,
                grant_plan TEXT,
                max_uses INTEGER,
                current_uses INTEGER DEFAULT 0,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        # Insert default promo codes
        cur.execute('''
            INSERT INTO promo_codes (code, grant_days, grant_plan, max_uses)
            VALUES ('ZEROMONEY', 3650, 'lifetime', 1000)
            ON CONFLICT (code) DO NOTHING;
        ''')
        cur.execute('''
            INSERT INTO promo_codes (code, discount_percent, max_uses)
            VALUES ('FIFTYFIFTY', 50, 10000)
            ON CONFLICT (code) DO NOTHING;
        ''')
        
        # Indexes for performance
        cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);')
        
        # Migration: Add missing columns to existing tables (PostgreSQL compatible)
        migration_columns = [
            ("users", "display_name", "TEXT"),
            ("users", "onboarding_done", "BOOLEAN DEFAULT FALSE"),
        ]
        for table, column, col_type in migration_columns:
            try:
                # Check if column exists first
                cur.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = %s AND column_name = %s
                """, (table, column))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")
                    logger.info(f"✅ Added column {table}.{column}")
            except Exception as migration_err:
                logger.warning(f"Migration note for {table}.{column}: {migration_err}")
        
        conn.commit()
        _db_initialized = True
        logger.info("✅ Database initialized successfully")
        return True
        
    except Exception as e:
        try:
            conn.rollback()
        except:
            pass
        logger.error(f"❌ Database initialization error: {e}")
        return False
    finally:
        db_pool.put_conn(conn)

# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'index'

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=Config.GOOGLE_CLIENT_ID,
    client_secret=Config.GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

class User(UserMixin):
    def __init__(self, data: dict):
        self.id = data['id']
        self.email = data['email']
        self.name = data.get('name', '')
        self.avatar_url = data.get('avatar_url')
        self.cv_content = data.get('cv_content')
        self.display_name = data.get('display_name')
        self.onboarding_done = data.get('onboarding_done', False)
        self.sub_plan = data.get('sub_plan', 'free')
        self.sub_expires = data.get('sub_expires')
        self.target_lang = data.get('target_lang', 'English')
        self.interface_lang = data.get('interface_lang', 'en')
        self.level = data.get('level', 'Intermediate')
        self.voice_speed = data.get('voice_speed', 1.0)
        self.theme = data.get('theme', 'dark')
        self.monthly_questions = data.get('monthly_questions', 0)
        self.monthly_reset_date = data.get('monthly_reset_date')
        self.total_questions = data.get('total_questions', 0)
    
    @property
    def is_paid(self) -> bool:
        if self.sub_plan == 'lifetime':
            return True
        return self.sub_expires and self.sub_expires > datetime.datetime.now()
    
    @property
    def can_ask_question(self) -> bool:
        """Check if user can ask a question (paid or free with remaining quota)"""
        if self.is_paid:
            return True
        # Reset monthly count if needed
        today = datetime.date.today()
        if self.monthly_reset_date and self.monthly_reset_date.month != today.month:
            return True  # Will reset on next question
        return self.monthly_questions < Config.FREE_MONTHLY_QUESTIONS
    
    @property
    def remaining_questions(self) -> int:
        if self.is_paid:
            return -1  # Unlimited
        return max(0, Config.FREE_MONTHLY_QUESTIONS - self.monthly_questions)

@login_manager.user_loader
def load_user(user_id):
    result = db_execute(
        "SELECT * FROM users WHERE id = %s", 
        (user_id,), 
        fetchone=True
    )
    if result:
        return User(dict(result))
    return None

def increment_user_questions(user_id: int):
    """Increment question count and handle monthly reset"""
    db_execute("""
        UPDATE users 
        SET monthly_questions = CASE 
            WHEN EXTRACT(MONTH FROM monthly_reset_date) != EXTRACT(MONTH FROM CURRENT_DATE) 
            THEN 1 
            ELSE monthly_questions + 1 
        END,
        monthly_reset_date = CURRENT_DATE,
        total_questions = total_questions + 1,
        last_active = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (user_id,), commit=True)

# ═══════════════════════════════════════════════════════════════════════════════
# AI FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

@cache.memoize(timeout=3600)
def generate_tts(text: str, speed: float = 1.0) -> Optional[str]:
    """Generate TTS audio with caching"""
    try:
        # Limit text length
        text = text[:2000]
        speed = max(0.25, min(4.0, float(speed)))
        
        response = openai_client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=text,
            speed=speed
        )
        return base64.b64encode(response.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None

def analyze_turn(session_data: dict, transcription: str, history: list, file_context: str = "") -> dict:
    """Analyze user turn and generate coach response"""
    category = session_data.get('category', 'job_interview')
    language = session_data.get('language', 'English')
    coach = COACHES.get(category, COACHES['job_interview'])
    
    system_prompt = f"""
{coach['prompt']}

=== CRITICAL LANGUAGE INSTRUCTION ===
The user has chosen to practice: **{language}**.
You MUST speak ONLY in **{language}**. This is non-negotiable.
Even if the user speaks English or makes mistakes, ALWAYS reply in {language} to immerse them.
Exception: You may briefly explain a complex grammar rule in English if the user is a complete beginner, but keep responses 90%+ in {language}.

=== SESSION CONTEXT ===
Target Language: {language}
User Context: {session_data.get('context_data', 'None provided')}
Uploaded Document: {file_context[:3000] if file_context else 'None'}

=== RESPONSE RULES ===
1. ALWAYS respond in {language} - THIS IS THE MOST IMPORTANT RULE
2. Stay 100% in character as {coach['name']}
3. Be engaging, helpful, and push the user to improve
4. NEVER say goodbye or end the conversation
5. ALWAYS end with a question or prompt to continue
6. Keep responses concise but valuable (2-4 paragraphs max)
7. Correct the user's mistakes naturally within your response

=== OUTPUT FORMAT ===
Respond with valid JSON only:
{{
    "coach_response_text": "Your response in {language} (MANDATORY)",
    "phonetic_simple": "Simple phonetic pronunciation guide for your response",
    "phonetic_ipa": "IPA pronunciation of your response",
    "transcription_user": "{transcription}",
    "analysis": {{
        "mistakes": [
            {{"bad": "what user said wrong", "fix": "correct version in {language}", "explanation": "why (can be in English for clarity)"}}
        ],
        "masterclass_answer": "How a native {language} speaker would phrase what the user tried to say"
    }}
}}
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add recent history
    for h in history[-8:]:
        messages.append({"role": h["role"], "content": h["content"]})
    
    messages.append({"role": "user", "content": transcription})
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.8,
            max_tokens=1000
        )
        
        result = json.loads(response.choices[0].message.content)
        return result
        
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return {
            "coach_response_text": "I had a moment. Could you repeat that?",
            "analysis": {"mistakes": [], "masterclass_answer": ""}
        }

def generate_greeting(coach_key: str, language: str, context: str = "") -> str:
    """Generate personalized greeting from coach"""
    coach = COACHES.get(coach_key, COACHES['job_interview'])
    
    prompt = f"""You are {coach['name']}, {coach['role']}.

CRITICAL: The user wants to practice **{language}**.
Generate your greeting ENTIRELY in **{language}**. Do NOT use English at all.
Even "Hello" should be in {language} (e.g., "Bonjour" for French, "Hola" for Spanish, etc.)

Context from user: {context if context else 'None provided'}

Keep it to 2-3 sentences. Be in character. End with an engaging question in {language}."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=200
        )
        return response.choices[0].message.content
    except:
        return f"Hello! I'm {coach['name']}. How can I help you today?"

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES - Static & Auth
# ═══════════════════════════════════════════════════════════════════════════════

@app.before_request
def before_request():
    """Initialize database on first request"""
    if not hasattr(g, 'db_initialized'):
        if Config.DATABASE_URL:
            db_pool.init_pool(Config.DATABASE_URL, Config.DB_POOL_MIN, Config.DB_POOL_MAX)
            init_database()
        g.db_initialized = True

@app.route('/')
def index():
    """Serve main page with SSR data injection for faster loading"""
    import os
    
    # Try to find the HTML file
    possible_paths = [
        os.path.join(app.static_folder or 'static', 'index.html'),
        'static/index.html',
        '/app/static/index.html',
        os.path.join(os.path.dirname(__file__), 'static', 'index.html'),
    ]
    
    html_content = None
    for html_path in possible_paths:
        try:
            with open(html_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
                break
        except:
            continue
    
    # Fallback: use Flask's built-in static file serving
    if not html_content:
        try:
            return app.send_static_file('index.html')
        except:
            return "Server starting... Please refresh.", 503
    
    # Prepare user data for SSR
    user_data = {"logged_in": False}
    my_sessions = []
    
    try:
        if current_user.is_authenticated:
            user_data = {
                "logged_in": True,
                "id": current_user.id,
                "email": current_user.email,
                "name": current_user.name,
                "display_name": getattr(current_user, 'display_name', None),
                "avatar_url": current_user.avatar_url,
                "is_paid": current_user.is_paid,
                "can_ask": current_user.can_ask_question,
                "remaining_questions": current_user.remaining_questions,
                "target_lang": current_user.target_lang,
                "interface_lang": getattr(current_user, 'interface_lang', 'en'),
                "level": current_user.level,
                "voice_speed": current_user.voice_speed,
                "theme": current_user.theme,
                "onboarding_done": getattr(current_user, 'onboarding_done', False)
            }
            
            # Get user sessions
            conn = db_pool.get_conn()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("""SELECT session_id, category, language 
                                   FROM sessions WHERE user_id = %s 
                                   ORDER BY last_updated DESC LIMIT 50""", (current_user.id,))
                    rows = cur.fetchall()
                    if rows:
                        my_sessions = [{"session_id": r[0], "category": r[1], "language": r[2]} for r in rows]
                except Exception as e:
                    logger.warning(f"SSR sessions fetch error: {e}")
                finally:
                    db_pool.put_conn(conn)
    except Exception as e:
        logger.warning(f"SSR user data error: {e}")
    
    # Inject SSR data as a script tag right after <body>
    ssr_script = f'''<script>
    // SSR Data - Injected by Flask for instant loading
    const SSR_USER = {json.dumps(user_data)};
    const SSR_COACHES = {json.dumps(COACHES)};
    const SSR_LANGUAGES = {json.dumps({"target": SUPPORTED_LANGUAGES, "interface": INTERFACE_LANGUAGES})};
    const SSR_SESSIONS = {json.dumps(my_sessions)};
    </script>
    '''
    
    # Insert after <body> tag
    html_content = html_content.replace('<body>', '<body>\n' + ssr_script, 1)
    
    return html_content

@app.route('/<path:filename>')
def static_files(filename):
    """Serve static files"""
    # Skip certain paths
    if filename.startswith('.well-known/'):
        return '', 404
    
    try:
        return send_from_directory(app.static_folder or 'static', filename)
    except:
        # For SPA routing, return index for HTML-like routes
        return redirect('/')

@app.route('/auth/login')
def auth_login():
    """Initiate Google OAuth login"""
    try:
        redirect_uri = url_for('auth_callback', _external=True)
        return google.authorize_redirect(redirect_uri)
    except Exception as e:
        logger.error(f"Auth login error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/auth/callback')
def auth_callback():
    """Handle Google OAuth callback"""
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')
        
        if not user_info:
            return redirect('/?error=no_user_info')
        
        # Find or create user
        conn = db_pool.get_conn()
        if not conn:
            return redirect('/?error=db_error')
        
        try:
            cur = conn.cursor()
            
            # Check if user exists
            cur.execute("SELECT * FROM users WHERE google_id = %s OR email = %s", 
                       (user_info['sub'], user_info['email']))
            user_data = cur.fetchone()
            
            if user_data:
                # Update existing user
                cur.execute("""
                    UPDATE users SET 
                        google_id = %s,
                        name = %s,
                        avatar_url = %s,
                        last_active = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (user_info['sub'], user_info.get('name'), 
                      user_info.get('picture'), user_data['id']))
                conn.commit()
                user = User(dict(user_data))
            else:
                # Create new user
                cur.execute("""
                    INSERT INTO users (google_id, email, name, avatar_url)
                    VALUES (%s, %s, %s, %s)
                    RETURNING *
                """, (user_info['sub'], user_info['email'], 
                      user_info.get('name'), user_info.get('picture')))
                user_data = cur.fetchone()
                conn.commit()
                user = User(dict(user_data))
            
            login_user(user)
            return redirect('/')
            
        finally:
            db_pool.put_conn(conn)
            
    except Exception as e:
        logger.error(f"Auth callback error: {e}")
        return redirect(f'/?error=auth_failed&details={str(e)}')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/me')
def api_me():
    """Get current user info"""
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True,
            "id": current_user.id,
            "email": current_user.email,
            "name": current_user.name,
            "display_name": current_user.display_name,
            "avatar_url": current_user.avatar_url,
            "is_paid": current_user.is_paid,
            "can_ask": current_user.can_ask_question,
            "remaining_questions": current_user.remaining_questions,
            "target_lang": current_user.target_lang,
            "interface_lang": current_user.interface_lang,
            "level": current_user.level,
            "voice_speed": current_user.voice_speed,
            "theme": current_user.theme,
            "onboarding_done": current_user.onboarding_done
        })
    return jsonify({"logged_in": False})

@app.route('/api/coaches')
@cache.cached(timeout=3600)
def api_coaches():
    """Get all coaches configuration"""
    return jsonify(COACHES)

@app.route('/api/languages')
@cache.cached(timeout=3600)
def api_languages():
    """Get supported languages"""
    return jsonify({
        "target": SUPPORTED_LANGUAGES,
        "interface": INTERFACE_LANGUAGES
    })

@app.route('/api/my_sessions')
@login_required
def api_my_sessions():
    """Get user's sessions"""
    result = db_execute(
        """SELECT session_id, category, language, last_updated 
           FROM sessions WHERE user_id = %s 
           ORDER BY last_updated DESC LIMIT 50""",
        (current_user.id,),
        fetch=True
    )
    
    if result:
        return jsonify([dict(r) for r in result])
    return jsonify([])

@app.route('/api/update_profile', methods=['POST'])
@login_required
def api_update_profile():
    """Update user preferences"""
    data = request.json
    
    allowed_fields = ['target_lang', 'interface_lang', 'level', 'voice_speed', 'theme', 'display_name', 'onboarding_done']
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    
    if not updates:
        return jsonify({"error": "No valid fields to update"}), 400
    
    set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
    values = list(updates.values()) + [current_user.id]
    
    db_execute(
        f"UPDATE users SET {set_clause} WHERE id = %s",
        values,
        commit=True
    )
    
    return jsonify({"status": "ok"})

@app.route('/api/contact', methods=['POST'])
@login_required
@limiter.limit("5 per hour")
def api_contact():
    """Submit contact form"""
    data = request.json
    
    db_execute(
        """INSERT INTO contact_messages (user_id, name, email, subject, message)
           VALUES (%s, %s, %s, %s, %s)""",
        (current_user.id, data.get('name'), data.get('email'),
         data.get('subject'), data.get('message')),
        commit=True
    )
    
    return jsonify({"status": "ok"})

@app.route('/api/promo_code', methods=['POST'])
@login_required
def api_promo_code():
    """Apply promo code"""
    code = request.json.get('code', '').upper().strip()
    
    if not code:
        return jsonify({"status": "error", "message": "No code provided"})
    
    promo = db_execute(
        """SELECT * FROM promo_codes 
           WHERE code = %s AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
           AND (max_uses IS NULL OR current_uses < max_uses)""",
        (code,),
        fetchone=True
    )
    
    if not promo:
        return jsonify({"status": "error", "message": "Invalid or expired code"})
    
    promo = dict(promo)
    
    # Grant access
    if promo.get('grant_days') and promo.get('grant_plan'):
        expire_date = datetime.datetime.now() + datetime.timedelta(days=promo['grant_days'])
        db_execute(
            "UPDATE users SET sub_plan = %s, sub_expires = %s WHERE id = %s",
            (promo['grant_plan'], expire_date, current_user.id),
            commit=True
        )
        
        # Increment usage
        db_execute(
            "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = %s",
            (code,),
            commit=True
        )
        
        return jsonify({
            "status": "success",
            "message": f"🎉 Code applied! You now have {promo['grant_plan']} access!"
        })
    
    elif promo.get('discount_percent'):
        return jsonify({
            "status": "discount",
            "message": f"Code valid for {promo['discount_percent']}% discount!",
            "discount": promo['discount_percent']
        })
    
    return jsonify({"status": "error", "message": "Invalid code configuration"})

@app.route('/api/payment_success', methods=['POST'])
@login_required
def api_payment_success():
    """Handle payment success"""
    plan = request.json.get('plan')
    
    if plan == 'month':
        expire_date = datetime.datetime.now() + datetime.timedelta(days=30)
    elif plan == 'year':
        expire_date = datetime.datetime.now() + datetime.timedelta(days=365)
    else:
        return jsonify({"error": "Invalid plan"}), 400
    
    db_execute(
        "UPDATE users SET sub_plan = %s, sub_expires = %s WHERE id = %s",
        (plan, expire_date, current_user.id),
        commit=True
    )
    
    return jsonify({"status": "ok"})

# ═══════════════════════════════════════════════════════════════════════════════
# CHAT ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/upload_file', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def upload_file():
    """Handle file uploads (PDF, DOCX, TXT)"""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400
    
    try:
        filename = file.filename.lower()
        content = ""
        
        if filename.endswith('.pdf'):
            pdf_reader = PdfReader(BytesIO(file.read()))
            content = "\n".join([page.extract_text() or "" for page in pdf_reader.pages])
        elif filename.endswith('.docx'):
            doc = Document(BytesIO(file.read()))
            content = "\n".join([para.text for para in doc.paragraphs])
        elif filename.endswith(('.txt', '.md')):
            content = file.read().decode('utf-8')
        else:
            return jsonify({"error": "Unsupported file type"}), 400
        
        return jsonify({
            "status": "ok",
            "extracted_text": content[:10000]  # Limit to 10k chars
        })
        
    except Exception as e:
        logger.error(f"File upload error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/start_chat', methods=['POST'])
@login_required
def start_chat():
    """Start or resume a coaching session"""
    data = request.json
    session_id = data.get('session_id')
    category = data.get('category', 'job_interview')
    language = data.get('language', 'English')
    context_data = data.get('context_data', '')
    file_context = data.get('file_context', '')
    speed = float(data.get('speed', 1.0))
    resume = data.get('resume', False)
    
    # Check limits for non-paid users
    if not current_user.is_paid:
        if not current_user.can_ask_question:
            return jsonify({"error": "limit_reached"}), 403
    
    conn = db_pool.get_conn()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    
    try:
        cur = conn.cursor()
        
        history = []
        
        if resume:
            # Get existing session
            cur.execute("SELECT * FROM sessions WHERE session_id = %s AND user_id = %s",
                       (session_id, current_user.id))
            session_data = cur.fetchone()
            
            if session_data:
                # Get history
                cur.execute("""SELECT role, content FROM history 
                              WHERE session_id = %s ORDER BY timestamp""", (session_id,))
                history = [dict(h) for h in cur.fetchall()]
                
                # Update timestamp
                cur.execute("UPDATE sessions SET last_updated = CURRENT_TIMESTAMP WHERE session_id = %s",
                           (session_id,))
                conn.commit()
                
                return jsonify({
                    "session_id": session_id,
                    "history": history,
                    "resumed": True
                })
        
        # Create new session
        cur.execute("""
            INSERT INTO sessions (session_id, user_id, category, language, context_data)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE SET
                category = EXCLUDED.category,
                language = EXCLUDED.language,
                context_data = EXCLUDED.context_data,
                last_updated = CURRENT_TIMESTAMP
        """, (session_id, current_user.id, category, language, context_data))
        conn.commit()
        
        # Generate greeting
        greeting = generate_greeting(category, language, context_data)
        
        # Save greeting to history
        cur.execute("""
            INSERT INTO history (session_id, role, content)
            VALUES (%s, 'assistant', %s)
        """, (session_id, greeting))
        conn.commit()
        
        # Generate TTS
        audio_b64 = generate_tts(greeting, speed)
        
        return jsonify({
            "session_id": session_id,
            "coach_response_text": greeting,
            "audio_base64": audio_b64,
            "history": []
        })
        
    except Exception as e:
        logger.error(f"Start chat error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        db_pool.put_conn(conn)

@app.route('/analyze', methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def analyze():
    """Analyze user input and generate response"""
    session_id = request.form.get('session_id')
    speed = float(request.form.get('speed', 1.0))
    
    # Get transcription from audio or text
    transcription = ""
    
    if 'audio' in request.files:
        audio_file = request.files['audio']
        try:
            # Read content and wrap in BytesIO with name attribute
            audio_content = audio_file.read()
            audio_buffer = BytesIO(audio_content)
            audio_buffer.name = "audio.webm"
            
            audio_response = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_buffer
            )
            transcription = audio_response.text
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return jsonify({"error": "Transcription failed"}), 500
    else:
        transcription = request.form.get('text_input', '')
    
    if not transcription.strip():
        return jsonify({"error": "No input provided"}), 400
    
    # Get session data
    conn = db_pool.get_conn()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    
    try:
        cur = conn.cursor()
        
        # Get session
        cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
        session_data = cur.fetchone()
        
        if not session_data:
            return jsonify({"error": "Session not found"}), 404
        
        session_data = dict(session_data)
        language = session_data.get('language', 'English')
        
        # Check limits
        if not current_user.is_paid and language not in Config.FREE_LANGUAGES:
            if not current_user.can_ask_question:
                return jsonify({"error": "limit_reached"}), 403
        
        # Get history
        cur.execute("""SELECT role, content FROM history 
                      WHERE session_id = %s ORDER BY timestamp""", (session_id,))
        history = [dict(h) for h in cur.fetchall()]
        
        # Get file context if any
        file_context = session_data.get('context_data', '')
        
        # Analyze turn
        result = analyze_turn(session_data, transcription, history, file_context)
        
        # Save to history
        cur.execute("""
            INSERT INTO history (session_id, role, content, metadata)
            VALUES (%s, 'user', %s, %s)
        """, (session_id, transcription, json.dumps({"original": transcription})))
        
        cur.execute("""
            INSERT INTO history (session_id, role, content, metadata)
            VALUES (%s, 'assistant', %s, %s)
        """, (session_id, result.get('coach_response_text', ''), 
              json.dumps(result.get('analysis', {}))))
        
        # Update session timestamp
        cur.execute("UPDATE sessions SET last_updated = CURRENT_TIMESTAMP WHERE session_id = %s",
                   (session_id,))
        
        conn.commit()
        
        # Increment question count for non-paid users
        if not current_user.is_paid:
            increment_user_questions(current_user.id)
        
        # Generate TTS
        audio_b64 = generate_tts(result.get('coach_response_text', ''), speed)
        
        result['audio_base64'] = audio_b64
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Analyze error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        db_pool.put_conn(conn)

# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH & MONITORING
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/health')
def health_check():
    """Health check endpoint for load balancers"""
    try:
        # Test database
        result = db_execute("SELECT 1", fetchone=True)
        db_ok = result is not None
    except:
        db_ok = False
    
    return jsonify({
        "status": "healthy" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "timestamp": datetime.datetime.utcnow().isoformat()
    }), 200 if db_ok else 503

@app.route('/api/stats')
@cache.cached(timeout=60)
def api_stats():
    """Public stats endpoint"""
    result = db_execute(
        "SELECT COUNT(*) as users FROM users",
        fetchone=True
    )
    
    total_users = result['users'] if result else 0
    
    return jsonify({
        "total_users": total_users,
        "coaches": len(COACHES),
        "languages": len(SUPPORTED_LANGUAGES)
    })

# Error handlers
@app.errorhandler(404)
def not_found(e):
    # For API routes, return JSON error
    if request.path.startswith('/api/'):
        return jsonify({"error": "Not found"}), 404
    # For other routes, redirect to home (SPA behavior)
    return redirect('/')

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Internal error: {e}")
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(429)
def rate_limit_error(e):
    return jsonify({"error": "Rate limit exceeded. Please wait a moment."}), 429

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    logger.info(f"🚀 Starting Infinity Coach on port {port}")
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_ENV') != 'production')