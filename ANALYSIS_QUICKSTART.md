# Draft Analysis Flow - Quick Start Guide

## What's New

The leads project now includes **comprehensive draft analysis** that evaluates email quality across 8 dimensions on a 0-10 scale:

1. ✉️ **Personalization** — How specific to this business?
2. 💔 **Pain Points** — How well does it address their problems?
3. 🎯 **CTA Hook** — How compelling is the call to action?
4. 💼 **Business Relevance** — How well does it fit their model?
5. 🤝 **Authenticity** — Does it sound human or templated?
6. 📖 **Clarity** — Is it crystal clear what you're offering?
7. 🛡️ **Trust Signals** — Does it build credibility?
8. ⭐ **Overall Quality** — Likely to get a response?

---

## Setup

No additional setup needed! Analysis is **automatically included** when you run `run_agents.py`.

### Environment Variables

The analysis uses the same LLM model configured for research & drafting:

```bash
# Already in your .env:
export LLM_MODEL=nemo_free        # ← Change this to use different model
export LITELLM_API_KEY=sk-...
export LITELLM_BASE_URL=http://127.0.0.1:4000
```

To use a different model for analysis, update `LLM_MODEL` in your `.env`:

```bash
# Example: use Claude or GPT-4o instead
export LLM_MODEL=gpt-4o           # Use OpenAI's latest
export LLM_MODEL=claude-3-opus    # Use Claude 3 Opus
```

---

## How It Works

### Integrated in run_agents.py

When you run the main orchestration flow:

```python
from flow.run_agents import run_agents

result = await run_agents(
    tiers=['hot', 'warm'],
    batch_size=10,
)
```

**Each business that gets drafted will be automatically analyzed:**

```
Researching 10 businesses...
✓ Business A: researched
  ✓ Business A: drafted
    ✓ Business A: analyzed
      personalization=8.5, pain_points=8.0, cta=7.5, overall=8.0
✓ Business B: researched
  ✓ Business B: drafted
    ✓ Business B: analyzed
      personalization=6.5, pain_points=5.0, cta=6.0, overall=5.5
```

### Output Includes Analysis

Each business outcome in the results includes:

```python
{
    'business_id': 'abc123',
    'business_name': 'Acme Dog Training',
    'draft': {
        'subject': '...',
        'body': '...',
        'selected_emails': [...]
    },
    'analysis': {
        'personalization_score': 8.5,
        'personalization_rationale': 'References their specific training approach...',
        'pain_points_score': 8.0,
        'pain_points_rationale': 'Directly addresses their new client acquisition challenge...',
        # ... all 8 scores + rationales
        'key_strengths': [
            'Highly specific to their training niche',
            'Strong understanding of their client pain points'
        ],
        'improvement_areas': [
            'Could include specific success example or metric',
            'CTA could create more urgency'
        ]
    }
}
```

---

## Standalone Batch Analysis

Analyze existing drafts without re-drafting:

```python
from flow.analyze_drafts import analyze_batch_drafts

# Prepare your drafts
drafts = [
    {
        'business': {'id': '1', 'business_name': 'Acme Dogs', ...},
        'research': {'personalization_hook': '...', 'pain_signals': [...], ...},
        'draft': {'subject': '...', 'body': '...'}
    },
    # ... more drafts
]

# Analyze the batch
results = await analyze_batch_drafts(drafts, output_format='detailed')

# Get aggregate statistics
print(f"Average Personalization: {results['score_aggregates']['personalization_score']['mean']:.1f}")
print(f"Average Overall Quality: {results['score_aggregates']['overall_quality_score']['mean']:.1f}")
```

### From JSON File

```python
from flow.analyze_drafts import analyze_batch_drafts

# Load and analyze from JSON
results = await analyze_batch_drafts(json_input_file='my_drafts.json')
```

---

## Understanding the Scores

### Interpretation Guide

```
0-3   = Significant Issues (needs major rework)
4-6   = Decent but Improvable (decent, room for growth)
7-8   = Strong (likely to work well)
9-10  = Exceptional (excellent response potential)
```

### What to Optimize For

**To maximize response rate, prioritize:**

1. **Personalization** (7+) — They need to know you researched them
2. **Pain Points** (7+) — They need to feel understood
3. **Authenticity** (7+) — They need to believe it's real
4. **CTA Hook** (7+) — They need a reason to respond
5. **Clarity** (8+) — They need to understand the offer immediately

---

## Quick Tips

### High-Scoring Templates (What Works)

- **Personalization**: Reference their specific services, location, or recent actions
- **Pain Points**: Start with "I noticed you [specific observation]" + "which means [pain signal]"
- **CTA Hook**: Create curiosity, offer specific benefit, or imply exclusivity
- **Authenticity**: Use conversational language, shorter sentences, personal voice
- **Overall Quality**: Combine personalization + pain understanding + clear, simple offer

### Common Issues & Fixes

| Issue | Score | Fix |
|-------|-------|-----|
| Generic language | Low Personalization | Add specific business details |
| No reason to respond | Low CTA Hook | Add specificity or create curiosity |
| Too corporate | Low Authenticity | Use conversational, shorter sentences |
| Unclear offer | Low Clarity | Simplify message, remove jargon |
| No proof | Low Trust Signals | Add specific example or result |

---

## Accessing Historical Analysis

### Prefect Variables

Each analysis is stored as a Prefect variable:

```bash
# View in Prefect UI at:
http://localhost:4200/variables/analysis_agent_output_<business_id>
```

### Flow Run Results

Check the analysis in returned outcomes:

```python
results = await run_agents(batch_size=10)
for outcome in results['outcomes']:
    if outcome['analysis_status'] == 'analyzed':
        analysis = outcome['analysis']
        print(f"{outcome['business_name']}: {analysis['overall_quality_score']:.1f}/10")
```

---

## Debugging Analysis Issues

If analysis fails (status='error'):

1. **Check LLM connection**
   ```bash
   curl http://127.0.0.1:4000/v1/models
   ```

2. **Check model name**
   ```bash
   echo $LLM_MODEL
   ```

3. **Check API key**
   ```bash
   echo $LITELLM_API_KEY
   ```

4. **Run verbose**
   ```bash
   export LLM_DEBUG=true
   python -m prefect run flow/run_agents.py
   ```

---

## Advanced: Model Selection

Want to use different models for different tasks?

Currently, analysis uses the same `LLM_MODEL` as research/drafting. To use different models:

1. Update `.env` to change `LLM_MODEL` for all tasks
2. Or modify `flow/agents/analysis.py` to read a separate `ANALYSIS_MODEL` variable

Example:

```python
# In flow/llm.py, add:
ANALYSIS_MODEL = os.environ.get('ANALYSIS_MODEL', os.environ.get('LLM_MODEL'))
```

```bash
# In .env:
export LLM_MODEL=nemo_free
export ANALYSIS_MODEL=gpt-4o  # Use better model for analysis
```

---

## Next Steps

1. **Run with integration**: `python -m prefect run flow/run_agents.py`
2. **Check scores**: Look at `analysis` field in results
3. **Iterate templates**: Use scores to guide copy improvements
4. **Track trends**: Monitor score distributions over time
5. **A/B test**: Analyze different approaches and compare

---

## Full Documentation

For detailed scoring rubrics and interpretation guidance, see:
👉 `ANALYSIS_METRICS.md`

For technical implementation details:
👉 `flow/agents/analysis.py`
👉 `flow/analyze_drafts.py`
