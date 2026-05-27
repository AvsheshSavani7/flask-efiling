### Email subject for each case type ###
# ACCC:
    New Case:
    Matched: subject = f"[FRMD] ACCC Case (New) – {case_number}: {title}"
    Unmatched: subject = f"[FRUD] ACCC Case (USA-Related) – {case_number}"

    Updated Case:
    subject = f"[FRMD] ACCC Case (Updated) – {case_number}: {title}"

# CADE Brazil:
    
    New Case:
    Matched: subject = f"[FRMD] CADE Brazil Regulatory (New) – {target} / {acquirer}"
    Unmatched: subject = f"[FRUD] CADE Brazil (USA-Related) – {process}"

    Update:
    subject = f"[FRMD] CADE Brazil (Updated) – {new_records_count} New Record(s) – {target} / {acquirer}"

# SAMR China public notice:
    New Case:
    Matched: subject = f"[FRMD] SAMR China Regulatory (New) – {target} / {acquirer}"
    Unmatched: subject = f"[FRUD] SAMR China Public Notice (USA-Related) – {title_en[:60]}"

    # SAMR China conditional approval:
    New Case:
    Matched: subject = f"[FRMD] SAMR China Conditional Approval (New) – {target} / {acquirer}"
    Unmatched: subject = f"[FRUD] SAMR China Conditional Approval (USA-Related) – {title_en[:60]}"

# SAMR China unconditional approval:
    New Case:
    Matched:  subject = f"[FRMD] SAMR China Unconditional Approval (New) – {target} / {acquirer}"
    Unmatched: subject = f"[FRUD] SAMR China Unconditional Approval (USA-Related) – {usa_company}"


# UK CMA mergers:
    New Case:
    Matched: subject = f"[FRMD] UK CMA Merger Case(New) – {target} / {acquirer}"
    
    Unmatched: subject = f"[FRUD] UK CMA Merger Case (USA-Related) – {title[:50]}"
    Update:
    subject = f"[FRMD] UK CMA Merger Case (Updated) – {target} / {acquirer}"

#German Bundeskartellamt initial filing:
    New Case:
    Matched: subject = f"[FRMD] German Bundeskartellamt Initial Filing (New) – {target} / {acquirer}"
    
    Update:
    subject = f"[FRMD] German Bundeskartellamt Initial Filing (Updated) – {target} / {acquirer}"


#German Bundeskartellamt press release:
    New Case:
    Matched: subject = f"[FRMD] German Bundeskartellamt Press Release (New) – {target} / {acquirer}"

    Update:
    subject = f"[FRMD] German Bundeskartellamt Press Release (Updated) – {target} / {acquirer}"

#EC Merger Case:
    New Case:
    Matched: subject = f"[FRMD] EC Merger Case (New) – {target} / {acquirer}"
    Unmatched: subject = f"[FRUD] EC Merger Case (USA-Related) – {case_num}: {companies_str}"

    Update:
    subject = f"[FRMD] EC Merger Case (Updated) – {case_number}: {case_title}"

#EC Foreign Subsidies Case:
    New Case:
    Matched: subject = f"[FRMD] EC Foreign Subsidies Case (New) – {target} / {acquirer}"

    Update:
    subject = f"[FRMD] EC Foreign Subsidies Case (Updated) – {case_number}: {case_title}"
  
  
#FTC
    New Case:
    Matched: subject = f"[RFTCMD] FTC Early Termination (New) – {target} / {acquirer}"

    Unmatched: subject = f"[RFTCUD] FTC Early Termination (USA-Related) – {case_id}"

#New NZ ComCom case:
    Matched: subject = f"[FRMD] NZ Case (New) – {case_number}: {title}"
    Unmatched: subject = f"[FRUD] NZ Case (USA-Related) – {case_number}"
  

    Update:
    subject = f"[FRMD] NZ Case (Updated) – {case_number}: {target} / {acquirer}"

#canada competition bureau:
    New Case:
    Matched: subject = f"FRMD: Canada Competition Bureau (New) – {target} / {acquirer}"
    
    
    Unmatched: subject = f"FRUD: Canada Competition Bureau (USA-Related)"

    Update:
    subject = f"FRMD: Canada Competition Bureau (Updated) – {target} / {acquirer}"

    


### Active routes and files Forign Filings ###

    EC:
        File: new_ec_cases_html.py
        Route:"/ec-cases-html-register"

        File: new_ec_cases_update_monitor.py
        Route:"/ec-cases-html-update-monitor"

    FS:
        File: new_fs_cases_html.py
        Route:"/new-fs-cases-html-register"

        File: new_fs_cases_html_update_monitor.py
        Route:"/new-fs-cases-html-update-monitor"

    ACCC:

        File: accc_cases_register.py
        Route:"/new-accc-cases-register"

        File: accc_cases_update_monitor.py
        Route:"/new-accc-cases-update-monitor"

        File: accc_waiver_register.py
        Route:"/new-accc-waiver-register"


    CADE Brazil:

        File: cade_cases_register.py
        Route:"/new-cade-cases-register"

        File: cade_cases_update_monitor.py
        Route:"/new-cade-cases-update-monitor"

    FTC:
        File: ftc_cases_scraper.py
        Route:"/ftc-early-termination-scraper"

    NZ ComCom:
        File: nz_comcom_case_register_to_db.py
        Route:"/new-nz-comcom-case-register-to-db"

        File: nz_cases_update_monitor.py
        Route:"/new-nz-cases-update-monitor"

    Canada Competition Bureau:
        File: canada_cases_register.py
        Route:"/new-canada-cases-register"

        File: canada_cases_update_monitor.py
        Route:"/new-canada-cases-update-monitor"

    UK :
        File: new_uk_cma_mergers_scraper_atom.py
        Route:"/new-uk-cma-scraper"

        File: new_uk_cma_mergers_update_monitor.py
        Route:"/new-uk-cma-update-monitor"

    German Bundeskartellamt:
        File: bundeskartellamt_initial_proxy.py
        Route:"/bundeskartellamt-initial"

        File: bundeskartellamt_update_monitor.py
        Route:"/bundeskartellamt-update-monitor"

        #Old way
        File: bundeskartellamt_press_release.py
        Route:"/bundeskartellamt-press-release"


    China SAMR:
        File: new_samr_unconditional_approval_db.py
        Route:"/new-samr-unconditional-scraper"

        File: new_samr_conditional_approval_db.py
        Route:"/new-samr-conditional-scraper"

        File: new_samr_public_notice_db.py
        Route:"/new-samr-public-scraper"



Docket:
  STB:
   /scrape/
   /analyze-docket

  FCC:
  /analyze-docket
  /fcc-scraper

  NE_PSC:
  /ne-psc-scraper
  /analyze-docket

  NM_PRC:
  /nm-prc-download-extract

  MT_PSC:
  /mt-psc-scraper

  SD_PUC:
  /sd-puc-scraper
