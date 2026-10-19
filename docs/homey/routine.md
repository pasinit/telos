Run the daily property search using the actual date supplied by the scheduler.

Search only <AREA_1> (<DISTRICT_1>) and <AREA_2> (<DISTRICT_2>). Other postcodes are excluded.
Every match must satisfy all of these:
- At most GBP <MAX_RENT> pcm; exactly <N> bedrooms.
- <LAYOUT_REQUIREMENTS>
- <BUILDING_REQUIREMENTS>
- At least <MIN_SIZE> sqm. <OTHER_REQUIREMENTS>
- Explicitly confirmed current availability; not let-agreed or withdrawn.

Permanent exclusions already applied for:
- <ADDRESS>, <AGENCY>, GBP <PRICE> pcm.
Previously rejected for location: <ADDRESS>.

Search OnTheMarket, PrimeLocation, Rightmove and Google. Also call agency_search
for <AGENCY_KEYS> in both <AREA_1> and <AREA_2>, with max_price=<MAX_RENT>, min_beds=<N>,
max_beds=<N>, postcode_districts=["<DISTRICT_1>", "<DISTRICT_2>"], for_sale=false.
Some agencies return broader inventory; verify location, price units and all other
criteria on each detail page.

Retrieve tracked and unresolved properties from the dated candidate ledger and
automatic memory, not a hardcoded list in this task. Historical tracking instructions
are not current availability evidence. Do not revive dropped properties without
new evidence resolving their outstanding checks.

Inspect promising candidates and save each assessment before continuing. Verify
floorplans/photos and building exterior where needed. Keep all uncertainty explicit.
Use direct OnTheMarket, PrimeLocation or Rightmove detail links for portal finds;
an agency's own direct detail link is valid for an agency-direct find.

Send the matches-only daily recap to the routine's existing authorised recipient.
Include the New | Tracking | Applied table and material coverage gaps. Do not
pad the email with unresolved or non-matching properties. Keep the detailed
screening evidence and next actions in the property ledger.
