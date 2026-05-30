"""
LLM prompt templates for the ReadingTime agent.

All prompts are defined here as module-level constants.  No other module
should contain raw prompt strings — import from here instead.

Each prompt includes explicit output format instructions so the LLM returns
parseable JSON or plain text as required.
"""

# ---------------------------------------------------------------------------
# Query Generation — turn user profile into search queries
# ---------------------------------------------------------------------------

QUERY_GENERATION_PROMPT = """You are a book recommendation engine. Given a user's reading profile, generate 3-5 search queries that would find books they would enjoy.

## User Profile
- Liked tags (genres they enjoy): {liked_tags}
- Liked authors: {liked_authors}
- Neutral tags (genres they don't prefer): {neutral_tags}
- Preferred language: {lang_pref}

## Instructions
1. Generate 3-5 diverse search queries — mix of authors, genres, themes, and styles.
2. Avoid queries that directly match neutral tags.
3. Favor queries that combine liked tags with new adjacent genres.
4. Keep each query under 60 characters — these are search engine queries, not essays.

## Output Format
Return ONLY a JSON array of strings. No explanation, no markdown.

Example: ["classic Russian literature", "psychological thriller 19th century", "existential fiction"]
"""

# ---------------------------------------------------------------------------
# Candidate Scoring — rank book candidates against profile
# ---------------------------------------------------------------------------

SCORING_PROMPT = """You are a literary taste matcher. Given a user's reading profile and a list of book candidates, score each candidate from 1-10 based on how well it matches their taste.

## User Profile
- Liked tags: {liked_tags}
- Liked authors: {liked_authors}
- Neutral tags (avoid): {neutral_tags}
- Language preference: {lang_pref}

## Candidates
{candidates_text}

## Scoring Criteria
- 10 = Perfect match: matches multiple liked tags and/or a liked author
- 7-9 = Strong match: matches at least one liked tag, no neutral tags
- 5-6 = Moderate match: no strong signals either way
- 3-4 = Weak match: matches a neutral tag or genre the user avoids
- 1-2 = Poor match: heavily matches neutral tags

## Output Format
Return ONLY a JSON object mapping source_id to score. No explanation, no markdown. Example:

{{"gutenberg:1234": 8, "ol:somebook": 5}}
"""

# ---------------------------------------------------------------------------
# Summary Generation — create book summary and recommendation
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """You are a literary critic writing for a curious reader. Write a book summary and a personalized recommendation based on the provided context.

## Book Information
- Title: {title}
- Author: {author}
- Language: {language}
- Tags/Genres: {tags}

## Book Excerpt (first ~2000 characters)
{excerpt}

## Instructions
1. Write a summary of approximately 300 characters (in {summary_lang}).
2. Write one sentence explaining why someone who enjoys {liked_tags} would like this book (in Chinese).
3. Be specific — reference themes, style, or comparisons to other authors when relevant.

## Output Format
Return ONLY a JSON object with keys "summary" and "recommendation". No markdown, no extra text.

Example:
{{"summary": "A gripping psychological thriller...", "recommendation": "如果你喜欢心理悬疑和层层递进的叙事，这本书会让你想起《消失的爱人》的紧张节奏。"}}
"""

# ---------------------------------------------------------------------------
# Feature Extraction — extract rich features from book metadata for profiling
# ---------------------------------------------------------------------------

FEATURE_EXTRACTION_PROMPT = """You are a book cataloger. Given a book's basic metadata, extract a rich set of features for a recommendation system.

## Book Information
- Title: {title}
- Author: {author}
- Existing tags: {existing_tags}
- Description: {description}

## Instructions
1. Identify the primary and secondary genres (at most 5 total).
2. Estimate the literary era (e.g., "19th century", "modern", "contemporary").
3. Note the writing style (e.g., "literary", "genre", "experimental", "accessible").
4. Identify themes (e.g., "redemption", "identity", "war", "love", "technology").
5. Suggest the target audience (e.g., "adult", "young adult", "scholarly").

## Output Format
Return ONLY a JSON object with these keys:
- "tags": [array of genre strings, lowercase]
- "era": string
- "style": string
- "themes": [array of theme strings]
- "audience": string

Example:
{{"tags": ["mystery", "psychological", "thriller"], "era": "20th century", "style": "literary", "themes": ["justice", "obsession", "memory"], "audience": "adult"}}
"""
