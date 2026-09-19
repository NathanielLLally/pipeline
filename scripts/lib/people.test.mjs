import { cleanName, extractCandidates, pickDecisionMaker } from "./people.mjs";
let pass=0, fail=0;
const ok=(c,m)=>{ if(c){pass++;console.log("ok   "+m);} else {fail++;console.log("FAIL "+m);} };

// --- cleanName ---
ok(cleanName("Ian Dunbar")==="Ian Dunbar","plain name");
ok(cleanName("Dr. Ian Dunbar")==="Ian Dunbar","honorific stripped");
ok(cleanName("Stephanie Zablah-Kruger")==="Stephanie Zablah-Kruger","hyphenated surname");
ok(cleanName("Kelly Gorman Dunbar")==="Kelly Gorman Dunbar","three-part name");
ok(cleanName("Stephanie Kruger KPA-CTP")==="Stephanie Kruger","credential stripped");
ok(cleanName("BOARD AND TRAIN")===null,"allcaps heading rejected");
ok(cleanName("Dog Training")===null,"business words rejected");
ok(cleanName("Our Team")===null,"nav text rejected");
ok(cleanName("J")===null,"bare initial rejected");
ok(cleanName("Puppy Kindergarten")===null,"program name rejected");
ok(cleanName("Privacy Policy")===null,"footer text rejected");

// --- the dog-owner trap ---
const dogOwner="Every dog owner wants a well behaved pet. We teach the dog owner how to lead.";
ok(extractCandidates(dogOwner,{pageKind:"about"}).length===0,"'dog owner' yields nothing");
ok(extractCandidates("We help pet owners in Austin.",{pageKind:"about"}).length===0,"'pet owners' yields nothing");
ok(extractCandidates("Sarah helps their owner build trust.",{pageKind:"about"}).length===0,"'their owner' yields nothing");

// --- real text from the crawl ---
const real1="(925)580-6410 szkruger@cooperativecaretraining.com Stephanie Zablah-Kruger (Owner) KPA-CTP, Low Stress Handling Certified";
const c1=extractCandidates(real1,{pageKind:"about",pageUrl:"https://x.com/about"});
ok(c1.some(c=>c.name==="Stephanie Zablah-Kruger"&&c.title==="Owner"&&c.confidence==="high"),"real: Name (Owner)");

const real2="My name is Donald Hutcherson and I want to tell you the story about how I got into dog training";
const c2=extractCandidates(real2,{pageKind:"about"});
ok(c2.some(c=>c.name==="Donald Hutcherson"&&c.confidence==="low"),"real: self-intro on about page");
ok(extractCandidates(real2,{pageKind:"pricing"}).length===0,"self-intro ignored off about page");

const real3="Founder and veterinarian Dr. Ian Dunbar revolutionized dog training";
const c3=extractCandidates(real3,{pageKind:"about"});
ok(c3.some(c=>c.name==="Ian Dunbar"&&c.title==="Founder"),"real: title-then-name with honorific");

const c4=extractCandidates("Michael Sandman, Owner and head trainer, has 25 years experience.",{pageKind:"about"});
ok(c4.some(c=>c.name==="Michael Sandman"&&c.title==="Owner"),"name, comma, title");

// --- title normalization ---
ok(extractCandidates("Jane Doe, Co-Founder, has led our puppy program since 2014.",{pageKind:"about"}).some(c=>c.title==="Co-founder"),"co-founder normalized");
ok(extractCandidates("Amy Ray is the general manager of our facility",{pageKind:"about"}).some(c=>c.title==="General Manager"),"general manager");

// --- pickDecisionMaker ---
const cands=[{name:"Jane Doe",title:"Owner",confidence:"medium",pattern:"name_is_title",page_kind:"about",source_url:"/about"}];
ok(pickDecisionMaker(cands,{personEmails:["jane.doe@biz.com"]}).confidence==="high","email corroboration promotes to high");
ok(pickDecisionMaker(cands,{personEmails:["info@biz.com"]}).confidence==="medium","role email does not promote");
ok(pickDecisionMaker(cands,{personEmails:["jdoe@biz.com"]}).confidence==="high","initial+surname email matches");

const two=[
 {name:"Alex Militar",title:"Owner",confidence:"high",pattern:"name_paren_title",page_kind:"team",source_url:"https://x.com/instructors"},
 {name:"Sam Reed",title:"Owner",confidence:"high",pattern:"name_paren_title",page_kind:"team",source_url:"https://x.com/instructors"}];
ok(pickDecisionMaker(two)===null,"tied candidates yield nothing");

const staff=[{name:"Alex Militar",title:"Owner",confidence:"high",pattern:"name_paren_title",page_kind:"team",source_url:"https://x.com/instructors"}];
ok(pickDecisionMaker(staff).confidence==="low","staff-page-only name demoted");
ok(pickDecisionMaker([])===null,"no candidates yields null");

const multi=[
 {name:"Jane Doe",title:"Owner",confidence:"medium",pattern:"name_is_title",page_kind:"about",source_url:"/about"},
 {name:"Jane Doe",title:"Founder",confidence:"medium",pattern:"title_then_name",page_kind:"home",source_url:"/"}];
const p=pickDecisionMaker(multi);
ok(p.name==="Jane Doe"&&p.corroboration.patterns.length===2,"two patterns corroborate one name");

// evidence quote must be a literal substring (Prompt 9 audit requirement)
const q=extractCandidates(real1,{pageKind:"about"})[0];
ok(real1.replace(/\s+/g," ").includes(q.evidence_quote),"evidence quote is verbatim");


// --- Regressions from the 2026-09-18 corpus audit ---
// Each of these was a real false positive (or a real name wrongly discarded) found by
// dumping the FULL high-confidence output rather than sampling its head.
const names = (t, o = { pageKind: "about", pageUrl: "/about" }) =>
  extractCandidates(t, o).map((c) => [c.name, c.title]);

ok(names("As Featured In Client Steph Curry Meet the Founder Training Programs for dogs").length === 0,
   "client testimonial is not the founder");
ok(names("forever home. Nancys Bio Jake Satterlee Vice President of Operations here").length === 0,
   "Vice President is not the decision maker");
ok(names("retiring from her demanding corporate Senior Vice President role, Karen went on").length === 0,
   "a past corporate job in a bio is not this business");
ok(names("Please contact Maria Maria Co-Founder about scheduling your consultation today").length === 0,
   "a repeated word is a rendering artifact, not a name");
ok(names("Behavior Specialist 15yrs Experience Scottsdale Greg Winters- PDT Owner/Behaviorist").length === 0,
   "trailing hyphen is stripped markup");
ok(names("Alliance Trupanion San Diego Humane Society Fresh Patch Pet Tested, Owner Approved").length === 0,
   "marketing copy does not pair into a name");
ok(names("Blog Contact About Peggy McCarty CTC Hound Haven owner Peggy McCarty here").every(([n]) => n !== "Hound Haven"),
   "business name is not the owner");

// The bare-space separator: two words adjacent to the title is evidence; three is not.
ok(names("Our facility is open daily. Gema Mays Owner Gema started the business in 2011")
     .some(([n, t]) => n === "Gema Mays" && t === "Owner"),
   "bare-space two-word name accepted");
ok(names("Contact us anytime. Hillary Ratcliff Pini, Owner, Best Friends Dog Training")
     .some(([n]) => n === "Hillary Ratcliff Pini"),
   "punctuation earns a three-word name");

// The article guard must anchor to the cleaned name, not the raw capture: "the"
// qualifies "Team", not the person, and anchoring it wrongly lost six real names.
ok(names("Meet the Team Zephyr Dippel Owner Bio coming soon Nia Stevens Owner and VP")
     .some(([n]) => n === "Zephyr Dippel"),
   "article guard does not eat a name behind page furniture");

// Seniority: the senior person must win, and Co-Owner must not be recorded as Owner.
{
  const t = "Learn about our leadership here. Valerie Fry Owner / CEO envelope Keisha Tucker Co-Owner / CFO";
  const cands = extractCandidates(t, { pageKind: "about", pageUrl: "/about" });
  ok(pickDecisionMaker(cands)?.name === "Valerie Fry",
     "senior person wins over a junior colleague");
  ok(cands.some((c) => c.name === "Keisha Tucker" && c.title === "Co-owner"),
     "Co-Owner is not recorded as sole Owner");
}

console.log(`\n${pass} passed, ${fail} failed`);

process.exit(fail?1:0);
