# Draft Analysis — Quick Reference Card

## 8 Scoring Dimensions (0-10 Scale)

### 1️⃣ Personalization
**How specific to THIS business?**
- 0-3: Generic, could send to anyone
- 4-6: Mentions name/industry, lacks depth
- 7-8: Shows knowledge of their business
- 9-10: Obviously unique to THIS specific business

**What Works:** Business name + specific service + pain signals

---

### 2️⃣ Pain Points
**How well does it address their problems?**
- 0-3: Misses the mark entirely
- 4-6: Vague pain references
- 7-8: Clearly articulates their pain
- 9-10: Perfect articulation of exact problems

**What Works:** Specific pain language + multiple signals + clear connection

---

### 3️⃣ CTA Hook
**How compelling is the call to action?**
- 0-3: No reason to respond
- 4-6: Weak appeal
- 7-8: Strong, creates interest
- 9-10: Recipient compelled to respond immediately

**What Works:** Specificity + urgency + clear benefit + next step

---

### 4️⃣ Business Relevance
**How well does it fit their business model?**
- 0-3: Irrelevant to their business
- 4-6: Tangentially relevant
- 7-8: Natural fit
- 9-10: Perfect match for their model

**What Works:** Understands revenue stream + acquisition pain + ROI fit

---

### 5️⃣ Authenticity
**Does it sound human or templated?**
- 0-3: Obviously form letter
- 4-6: Mix of genuine and templated
- 7-8: Authentic voice
- 9-10: Distinctly human, personal perspective

**What Works:** Conversational language + specific details + personal tone

---

### 6️⃣ Clarity
**Is it crystal clear?**
- 0-3: Confusing, hard to parse
- 4-6: Reasonably clear
- 7-8: Very clear
- 9-10: Anyone understands immediately

**What Works:** Simple language + clear structure + concrete examples

---

### 7️⃣ Trust Signals
**Does it build credibility?**
- 0-3: No proof or credentials
- 4-6: Some trust elements
- 7-8: Clear proof/credentials
- 9-10: Strong credibility indicators

**What Works:** Specific results + similar business examples + track record

---

### 8️⃣ Overall Quality
**Likelihood of response?**
- 0-3: Will be deleted immediately
- 4-6: Moderate response probability
- 7-8: High response probability
- 9-10: Very likely to generate interest

**What Works:** All elements aligned + recipient feels understood + motivated

---

## Score Distribution Guide

| Score | Interpretation | Action |
|-------|---|---|
| **0-3** | 🔴 Critical Issues | Rewrite from scratch |
| **4-5** | 🟠 Needs Major Work | Significant revisions needed |
| **6** | 🟡 Below Average | Several improvements needed |
| **7** | 🟢 Good | Can be effective |
| **8** | 🟢 Strong | Likely to work well |
| **9-10** | ✅ Excellent | Likely high response rate |

---

## Common Issues & Fixes

| Symptom | Likely Cause | Fix |
|---------|---|---|
| Low Personalization | Generic language | Add specific business details |
| Low Pain Points | Missing connection | Reference their actual pain signals |
| Low CTA Hook | Weak appeal | Make it specific, urgent, or curious |
| Low Authenticity | Robotic tone | Use conversational language, shorter sentences |
| Low Clarity | Too complex | Simplify, remove jargon, shorter message |
| Low Trust Signals | No proof | Add specific result or example |
| Low Relevance | Wrong business type | Ensure offer matches their revenue model |
| Low Overall Quality | Multiple issues | Fix lowest-scoring dimension first |

---

## Benchmark Targets

**For Strong Response Potential (7+):**
- ✅ Personalization: 7+
- ✅ Pain Points: 7+
- ✅ CTA Hook: 7+ (or 6+ if other scores compensate)
- ✅ Business Relevance: 7+
- ✅ Authenticity: 7+
- ✅ Clarity: 8+ (clear is critical)
- ✅ Trust Signals: 6+ (helpful but not essential)
- ✅ Overall Quality: 7+

**For Excellent Response Potential (8+):**
- ✅ All dimensions at 7+
- ✅ At least 3 dimensions at 8+
- ✅ No dimension below 6

---

## Usage Patterns

### Run Analysis
```bash
# Integrated (automatic)
python -m prefect run flow/run_agents.py

# Standalone
python -c "from flow.analyze_drafts import analyze_batch_drafts; ..."
```

### Read Results
```python
# In outcomes:
analysis = outcome['analysis']
score = analysis['overall_quality_score']
rationale = analysis['overall_quality_rationale']
strengths = analysis['key_strengths']
improvements = analysis['improvement_areas']
```

### Aggregate Stats
```python
# Get mean scores across batch
results = await analyze_batch_drafts(drafts)
mean_quality = results['score_aggregates']['overall_quality_score']['mean']
```

---

## Environment Variables

```bash
# Use different models:
export LLM_MODEL=nemo_free      # Default (lightweight)
export LLM_MODEL=gpt-4o         # OpenAI (powerful)
export LLM_MODEL=claude-3-opus  # Claude (excellent)
```

**Note:** Same model used for research, drafting, AND analysis.

---

## Key Insights

🎯 **Focus on personalization first**
- Generic emails underperform regardless of other factors
- Specificity signals real interest and research

💔 **Pain point connection is critical**
- Recipient must feel *understood*
- Reference their specific challenges, not generic problems

🤝 **Authenticity beats polish**
- Conversational > Corporate
- Human > Template
- Personal perspective > Generic offering

📖 **Clarity is often overlooked**
- Your offer must be immediately obvious
- Long emails get skimmed, then deleted

🛡️ **Trust signals build confidence**
- Don't need perfect proof, but need *something*
- Reference similar businesses helped > just claims

---

## Interpretation Examples

### High Overall Quality (8.5/10)
- ✅ "This email feels tailored to them"
- ✅ "They'll understand exactly what's being offered"
- ✅ "There's a compelling reason to respond"
- ✅ Expected response rate: 8-15%

### Moderate Overall Quality (6.0/10)
- ⚠️ "It's decent but nothing special"
- ⚠️ "They might respond, but not compelling"
- ⚠️ "Needs a few improvements to be strong"
- ⚠️ Expected response rate: 2-5%

### Low Overall Quality (3.5/10)
- 🔴 "Too generic"
- 🔴 "Missing their pain points"
- 🔴 "No reason to respond"
- 🔴 Expected response rate: <1%

---

## Strategy for Improvement

1. **Identify bottleneck** → Which dimension scores lowest?
2. **Fix bottleneck** → Apply targeted improvements
3. **Reanalyze** → Check if overall quality improved
4. **Iterate** → Repeat until reaching target

**Example:**
- Current: Personalization 5, Pain 7, CTA 6, Overall 5.5
- Bottleneck: Personalization (lowest)
- Fix: Add specific business details
- Reanalyze: Personalization 8, Pain 7, CTA 6, Overall 7.0 ✅

---

## Print & Keep Handy

Bookmark this file or print for quick reference while drafting and analyzing outreach!

For detailed rubrics: See `ANALYSIS_METRICS.md`
For practical guide: See `ANALYSIS_QUICKSTART.md`
