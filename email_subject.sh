# ACCC:
    New Case:
    Matched: subject = f"FRMD:ACCC (New) – {case_number}: {title}"
    Unmatched: subject = f"FRUD: ACCC Case (USA-Related) – {case_number}"

    Updated Case:
    subject = f"FRMD: ACCC Case (Updated) – {case_number}: {title}"

# CADE Brazil:
    New Case:
    Matched: subject = f"FRMD: CADE Brazil Regulatory (New) – {target} / {acquirer}"
    Unmatched: subject = f"FRUD: CADE Brazil (USA-Related) – {process}"

    Update:
    subject = f"FRMD: CADE Brazil (Updated) – {new_records_count} New Record(s) – {target} / {acquirer}"

# SAMR China public notice:
    New Case:
    Matched: subject = f"FRMD: SAMR China Regulatory (New) – {target} / {acquirer}"
    Unmatched: subject = f"FRUD: SAMR China Public Notice (USA-Related) – {title_en[:60]}"

    # SAMR China conditional approval:
    New Case:
    Matched: subject = f"FRMD: SAMR China Conditional Approval (New) – {target} / {acquirer}"
    Unmatched: subject = f"FRUD: SAMR China Conditional Approval (USA-Related) – {title_en[:60]}"

# SAMR China unconditional approval:
    New Case:
    Matched:  subject = f"FRMD: SAMR China Unconditional Approval (New) – {target} / {acquirer}"
    Unmatched: subject = f"FRUD: SAMR China Unconditional Approval (USA-Related) – {usa_company}"


# UK CMA mergers:
    New Case:
    Matched: subject = f"FRMD: UK CMA Merger Case(New) – {target} / {acquirer}"
    
    Unmatched: subject = f"FRUD: UK CMA Merger Case (USA-Related) – {title[:50]}"
    Update:
    subject = f"FRMD: UK CMA Merger Case (Updated) – {target} / {acquirer}"

#German Bundeskartellamt initial filing:
    New Case:
    Matched: subject = f"FRMD: German Bundeskartellamt Initial Filing (New) – {target} / {acquirer}"
    
    Update:
    subject = f"FRMD: German Bundeskartellamt Initial Filing (Updated) – {target} / {acquirer}"


#German Bundeskartellamt press release:
    New Case:
    Matched: subject = f"FRMD: German Bundeskartellamt Press Release (New) – {target} / {acquirer}"

    Update:
    subject = f"FRMD: German Bundeskartellamt Press Release (Updated) – {target} / {acquirer}"

#EC Merger Case:
    New Case:
    Matched: subject = f"FRMD: EC Merger Case (New) – {target} / {acquirer}"
    Unmatched: subject = f"FRUD: EC Merger Case (USA-Related) – {case_num}: {companies_str}"

    Update:
    subject = f"FRMD: EC Merger Case (Updated) – {case_number}: {case_title}"

#EC Foreign Subsidies Case:
    New Case:
    Matched: subject = f"FRMD: EC Foreign Subsidies Case (New) – {target} / {acquirer}"

    Update:
    subject = f"FRMD: EC Foreign Subsidies Case (Updated) – {case_number}: {case_title}"
  
  
#FTC
    New Case:
    Matched: subject = f"FRMD: FTC Early Termination (New) – {target} / {acquirer}"

    Unmatched: subject = f"FRUD: FTC Early Termination (USA-Related) – {case_id}"

#New NZ ComCom case:
    Matched: subject = f"FRMD: NZ Case (New) – {case_number}: {title}"
    Unmatched: subject = f"FRUD: NZ Case (USA-Related) – {case_number}"
  

    Update:
    subject = f"FRMD: NZ Case (Updated) – {case_number}: {target} / {acquirer}"

#canada competition bureau:
    New Case:
    Matched: subject = f"FRMD: Canada Competition Bureau (New) – {target} / {acquirer}"
    
    
    Unmatched: subject = f"FRUD: Canada Competition Bureau (USA-Related)"

    Update:
    subject = f"FRMD: Canada Competition Bureau (Updated) – {target} / {acquirer}"

    




    