# asicflow STA — PVT corner reports (STA_MODE=corner) and interactive shell (STA_MODE=shell).
#
# Shared setup: liberty, ODB, SDC, RC, parasitics, propagated clocks.
# Corner mode writes the report suite under STA_OUT (one directory per corner).
# Shell mode loads the same design then drops to the OpenROAD prompt.
#
# Required: STA_ODB STA_SDC STA_LIBERTY STA_RC_TCL STA_OUT
# Corner also requires STA_CORNER.

proc cfg {name default} {
    if {[info exists ::env($name)] && [string trim $::env($name)] ne ""} {
        return [string trim $::env($name)]
    }
    return $default
}

proc die {msg} {
    puts stderr "sta.tcl: $msg"
    exit 1
}

proc json_quote {value} {
    set value [string map [list "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "\\r" "\t" "\\t"] $value]
    return "\"$value\""
}

set MODE [string tolower [cfg STA_MODE corner]]

# ---------------------------------------------------------------- locate inputs
set ODB   [cfg STA_ODB ""]
set SDC   [cfg STA_SDC ""]
set LIBERTY [cfg STA_LIBERTY ""]
set RC_TCL  [cfg STA_RC_TCL ""]
set OUT   [cfg STA_OUT ""]
set CORNER [cfg STA_CORNER ""]
set MACRO_LIBS [split [string map {: { }} [cfg STA_MACRO_LIBS ""]]]
set PATHS [cfg STA_PATHS 20]
set STAGE_MAJOR [cfg STA_STAGE_MAJOR ""]
set stage [cfg STAGE ""]

if {$ODB eq "" || $SDC eq "" || $LIBERTY eq "" || $RC_TCL eq ""} {
    die "set STA_ODB STA_SDC STA_LIBERTY STA_RC_TCL (and STA_OUT)"
}
if {$OUT eq ""} { die "STA_OUT is required" }

set RESULTS [file dirname $ODB]
if {$stage eq ""} { set stage [file rootname [file tail $ODB]] }
if {$STAGE_MAJOR eq ""} {
    if {[regexp {^([0-9])_} $stage -> maj]} {
        set STAGE_MAJOR $maj
    } else {
        set STAGE_MAJOR 3
    }
}
set RUN_TAG [cfg RUN_TAG ""]
set PLATFORM [cfg PLATFORM ""]
set NICK [cfg DESIGN_NICKNAME ""]

foreach f [list $ODB $SDC $LIBERTY $RC_TCL] {
    if {![file exists $f]} { die "file not found: $f" }
}
file mkdir $OUT

set libs [list $LIBERTY]
foreach m $MACRO_LIBS { if {$m ne "" && [file exists $m]} { lappend libs $m } }

# ---------------------------------------------------------------- load design
foreach l $libs { read_liberty $l }
read_db $ODB
read_sdc $SDC
source $RC_TCL
set extra [cfg STA_EXTRA_TCL ""]
if {$extra ne ""} { source $extra }

set par [string tolower [cfg STA_PARASITICS auto]]
if {$par eq "auto"} {
    set spef [lindex [lsort [glob -nocomplain -directory $RESULTS 6_final*.spef]] 0]
    if {$stage eq "6_final" && $spef ne ""} {
        set par spef
    } elseif {$STAGE_MAJOR >= 5} {
        set have_routes 0
        if {[llength [info commands grt::have_routes]] > 0} { set have_routes [grt::have_routes] }
        set par [expr {$have_routes ? "grt" : "placement"}]
    } elseif {$STAGE_MAJOR >= 3} {
        set par placement
    } else {
        set par none
    }
}
switch $par {
    spef {
        if {![info exists spef] || $spef eq ""} {
            set spef [lindex [lsort [glob -nocomplain -directory $RESULTS *.spef]] 0]
        }
        if {$spef eq ""} { die "STA_PARASITICS=spef but no SPEF in $RESULTS" }
        read_spef $spef
        set par_desc "extracted SPEF ([file tail $spef])"
    }
    grt       { estimate_parasitics -global_routing; set par_desc "global-route estimate" }
    placement { estimate_parasitics -placement;      set par_desc "placement estimate" }
    none      { set par_desc "none (wire load only)" }
    default   { die "STA_PARASITICS must be auto|none|placement|grt|spef" }
}

set prop [string tolower [cfg STA_PROPAGATED auto]]
if {$prop eq "auto"} { set prop [expr {$STAGE_MAJOR >= 4}] }
if {$prop} { set_propagated_clock [all_clocks] }
set clk_desc [expr {$prop ? "propagated" : "ideal"}]

# ---------------------------------------------------------------- helpers
set nclk 0
catch { set nclk [llength [all_clocks]] }
if {$nclk == 0} {
    puts stderr "sta.tcl: WARNING: no clocks defined after read_sdc"
}

proc first_num {text} {
    if {[regexp -- {(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)} $text m]} { return $m }
    return "NA"
}
proc report_num {args} {
    sta::redirect_string_begin
    if {[catch {uplevel 1 $args}]} {
        sta::redirect_string_end
        return "NA"
    }
    return [first_num [sta::redirect_string_end]]
}
proc count_drc_viol {flag} {
    sta::redirect_string_begin
    if {[catch {report_check_types {*}$flag -violators}]} {
        sta::redirect_string_end
        return "NA"
    }
    set t [sta::redirect_string_end]
    return [regexp -all {VIOLATED} $t]
}
proc count_path_viol {path_delay} {
    sta::redirect_string_begin
    if {[catch {
        report_checks -path_delay $path_delay -slack_max 0 \
            -group_path_count 100000 -format end
    }]} {
        sta::redirect_string_end
        return "NA"
    }
    set t [sta::redirect_string_end]
    return [regexp -all {VIOLATED} $t]
}

# Write a report file from a Tcl body; never abort the suite on a soft failure.
proc write_rpt {file body} {
    sta::redirect_string_begin
    set rc [catch {uplevel 1 $body} err]
    set text [sta::redirect_string_end]
    if {$rc} { append text "\n(skipped: $err)\n" }
    set fh [open [file join $::OUT $file] w]
    puts -nonewline $fh $text
    close $fh
}

# Top-level ports can make get_nets return a NULL object with llength==1.
# Never pass that to get_pins — it emits STA-0101.
proc sta_net_ok {n} {
    if {$n eq "" || $n eq "NULL"} { return 0 }
    if {[catch {set name [get_full_name $n]}]} { return 0 }
    if {$name eq "" || $name eq "NULL"} { return 0 }
    return 1
}

# Create explicit path groups used by corner reports (idempotent if already present).
proc ensure_io_path_groups {} {
    catch { group_path -name reg2reg -from [all_registers] -to [all_registers] }
    catch { group_path -name in2reg  -from [all_inputs]    -to [all_registers] }
    catch { group_path -name reg2out -from [all_registers] -to [all_outputs] }
}

# Critical-path classification for RTL vs physical triage (top N setup paths).
# Prefer real clock path groups; skip asynchronous/recovery/unconstrained groups.
# PathEnd objects must be consumed immediately — do not stash them across finds.
# Dominant cause ∈ {cell_delay, net_delay, fanout, slew, capacitance, clock_uncertainty}.
proc write_critical_path_classification {file npaths} {
    set fh [open [file join $::OUT $file] w]
    puts $fh "# Critical path classification (setup, top $npaths by slack)"
    puts $fh "# stage=$::stage parasitics=$::par_desc clocks=$::clk_desc"
    puts $fh "# Excludes path groups: asynchronous, unconstrained, path delay, gated clock"
    puts $fh "# cell_delay includes launch CK->Q; logic_depth = combinational cells on path"
    puts $fh "# fanout = max pin count on any net driven by a path output pin"
    puts $fh "# dominant = cell_delay|net_delay|fanout|slew|capacitance|clock_uncertainty"
    puts $fh ""

    set skip [list asynchronous unconstrained {path delay} {gated clock}]
    set records {}
    set groups {}
    catch { set groups [sta::path_group_names] }
    set clock_groups {}
    foreach g $groups {
        if {[lsearch -exact $skip $g] < 0} { lappend clock_groups $g }
    }

    # Query each clock path group; fall back to ungrouped if none.
    set queries {}
    if {[llength $clock_groups] > 0} {
        foreach g $clock_groups { lappend queries [list $g 1] }
    } else {
        lappend queries [list "(default)" 0]
    }

    foreach q $queries {
        lassign $q gname use_group
        set paths {}
        if {$use_group} {
            if {[catch {
                set paths [find_timing_paths -path_delay max -path_group $gname \
                    -group_path_count $npaths -sort_by_slack]
            }]} continue
        } else {
            if {[catch {
                set paths [find_timing_paths -path_delay max \
                    -group_path_count $npaths -sort_by_slack]
            }]} continue
        }
        foreach pe $paths {
            set sp "NA"; set ep "NA"; set slack 0.0
            catch { set sp [get_full_name [get_property $pe startpoint]] }
            catch { set ep [get_full_name [get_property $pe endpoint]] }
            catch { set slack [get_property $pe slack] }

            set cell_ns 0.0
            set net_ns 0.0
            set depth 0
            set max_fo 0
            set max_slew 0.0
            set max_cap 0.0
            set uncert 0.0
            set hot_cell "NA"
            set hot_cell_ns 0.0
            set hot_net "NA"
            set hot_net_ns 0.0
            catch {
                set uncert [get_property $pe clock_uncertainty]
            }
            set pts {}
            catch { set pts [get_property $pe points] }
            set prev 0.0
            set idx 0
            foreach pt $pts {
                incr idx
                set pin [sta::Path_pin $pt]
                set arr [get_property $pt arrival]
                set dly [expr {double($arr) - double($prev)}]
                set dir ""
                catch { set dir [get_property $pin direction] }
                set pin_name ""
                catch { set pin_name [get_full_name $pin] }
                catch {
                    set sl [get_property $pin slew]
                    if {$sl > $max_slew} { set max_slew $sl }
                }
                catch {
                    set cp [get_property $pin capacitance]
                    if {$cp > $max_cap} { set max_cap $cp }
                }
                if {$dir eq "output"} {
                    set cell_ns [expr {$cell_ns + $dly}]
                    if {$idx > 1} { incr depth }
                    if {$dly > $hot_cell_ns} {
                        set hot_cell_ns $dly
                        set hot_cell $pin_name
                    }
                    # Ports have no net object; skip rather than query NULL.
                    catch {
                        set nets [get_nets -quiet -of_objects $pin]
                        set n [lindex $nets 0]
                        if {[sta_net_ok $n]} {
                            set fo [llength [get_pins -quiet -of_objects $n]]
                            if {$fo > $max_fo} { set max_fo $fo }
                        }
                    }
                } else {
                    set net_ns [expr {$net_ns + $dly}]
                    if {$dly > $hot_net_ns} {
                        set hot_net_ns $dly
                        set hot_net $pin_name
                    }
                }
                set prev $arr
            }

            # Dominant-cause heuristic (shares of path delay + design-rule pressure).
            set total [expr {$cell_ns + $net_ns}]
            set dominant "unknown"
            set share 0.0
            if {$total > 0} {
                if {$cell_ns >= $net_ns} {
                    set dominant "cell_delay"
                    set share [expr {$cell_ns / $total}]
                } else {
                    set dominant "net_delay"
                    set share [expr {$net_ns / $total}]
                }
            }
            if {$max_fo >= 20 && [expr {double($max_fo) / 64.0}] > $share} {
                set dominant "fanout"
                set share [expr {double($max_fo) / 64.0}]
            }
            if {$max_slew > 0.5 && [expr {$max_slew / 2.0}] > $share} {
                set dominant "slew"
                set share [expr {$max_slew / 2.0}]
            }
            if {$max_cap > 0.1 && [expr {$max_cap / 0.2}] > $share} {
                set dominant "capacitance"
                set share [expr {$max_cap / 0.2}]
            }
            if {$total > 0 && $uncert != 0 && [expr {abs($uncert) / $total}] > $share} {
                set dominant "clock_uncertainty"
                set share [expr {abs($uncert) / $total}]
            }

            lappend records [list $slack $gname $sp $ep $depth $cell_ns $net_ns \
                $max_fo $max_slew $max_cap $uncert $dominant $share $hot_cell $hot_net]
        }
    }

    if {[llength $records] == 0} {
        puts $fh "(no classified setup paths)"
        close $fh
        return
    }

    set records [lsort -real -index 0 $records]
    set i 0
    foreach rec $records {
        if {$i >= $npaths} break
        incr i
        lassign $rec slack pg sp ep depth cell_ns net_ns max_fo max_slew max_cap \
            uncert dominant share hot_cell hot_net
        puts $fh "Path $i:"
        puts $fh "  Startpoint:  $sp"
        puts $fh "  Endpoint:    $ep"
        puts $fh "  Path Group:  $pg"
        puts $fh "  Logic depth: $depth"
        puts $fh "  Cell delay:  [format %.4f $cell_ns] ns"
        puts $fh "  Net delay:   [format %.4f $net_ns] ns"
        puts $fh "  Fanout:      $max_fo"
        puts $fh "  Max slew:    [format %.4f $max_slew] ns"
        puts $fh "  Max cap:     [format %.4f $max_cap]"
        puts $fh "  Uncertainty: [format %.4f $uncert] ns"
        puts $fh "  Dominant:    $dominant ([format %.3f $share])"
        puts $fh "  Hot cell:    $hot_cell"
        puts $fh "  Hot net:     $hot_net"
        puts $fh "  Slack:       [format %.4f $slack] ns"
        puts $fh ""
    }
    close $fh
}

proc write_path_group_rpt {file group npaths} {
    set ::_pg_name $group
    set ::_pg_n $npaths
    write_rpt $file {
        puts "# path group: $::_pg_name"
        puts "# setup paths (top $::_pg_n)"
        puts ""
        catch {
            report_checks -path_delay max -path_group $::_pg_name \
                -format $::fmt -fields $::path_fields \
                -group_path_count $::_pg_n -digits 3
        }
        puts ""
        puts "=== worst slack ==="
        catch {
            report_checks -path_delay max -path_group $::_pg_name \
                -group_path_count 1 -format end -digits 4
        }
        puts ""
        puts "=== hold (top 5) ==="
        catch {
            report_checks -path_delay min -path_group $::_pg_name \
                -format end -group_path_count 5 -digits 3
        }
    }
}

# ================================================================= shell mode
# OpenROAD rejects a top-level `return`; end the script without `exit` so the
# interactive prompt stays up when invoked without -exit.
if {$MODE eq "shell"} {
    puts "============================================================"
    puts "sta-shell ready"
    puts "  stage=$stage  corner=[cfg STA_CORNER default]  parasitics=$par_desc"
    puts "  odb=$ODB"
    puts "  sdc=$SDC"
    puts "  liberty=[join $libs {, }]"
    puts "  out=$OUT  (reports not written in shell mode)"
    puts "  clocks=$clk_desc ($nclk defined)"
    puts "============================================================"
} elseif {$MODE eq "corner"} {

# ================================================================= corner mode
if {$CORNER eq ""} { die "STA_CORNER required in corner mode" }

set setup_ws  [report_num report_worst_slack -max -digits 4]
set hold_ws   [report_num report_worst_slack -min -digits 4]
set setup_tns [report_num report_tns -max -digits 4]
set hold_tns  [report_num report_tns -min -digits 4]
set wns       [report_num report_wns -digits 4]

set clk_period "NA"
catch {
    set clk_period [format %.4f [expr {
        [get_property [lindex [all_clocks] 0] period] * 1.0
    }]]
    if {$clk_period ne "NA" && $clk_period < 1e-3} {
        set clk_period [format %.4f [expr {$clk_period * 1e9}]]
    }
}

set period_min "NA"
set fmax_reg2reg "NA"
sta::redirect_string_begin
catch {report_clock_min_period}
set cp [sta::redirect_string_end]
if {[regexp {period_min\s*=\s*([0-9.]+)} $cp -> pm]} {
    set period_min $pm
    if {$pm > 0} { set fmax_reg2reg [format %.2f [expr {1000.0 / $pm}]] }
}

set setup_v [count_path_viol max]
set hold_v  [count_path_viol min]
set slew_v  [count_drc_viol {-max_slew}]
set cap_v   [count_drc_viol {-max_capacitance}]
set fanout_v [count_drc_viol {-max_fanout}]

# Per path-group WNS (handoff: don't blend async recovery into "the" WNS).
# report_checks -format end prints:  "... <slack> (VIOLATED|MET)" — not "slack (...)".
proc group_setup_ws {g} {
    sta::redirect_string_begin
    if {[catch {
        report_checks -path_delay max -path_group $g \
            -group_path_count 1 -format end -digits 4
    }]} {
        sta::redirect_string_end
        return "NA"
    }
    set t [sta::redirect_string_end]
    # Prefer the first endpoint-table slack; fall back to full-format wording.
    if {[regexp -- {(-?[0-9]+(?:\.[0-9]+)?)\s+\((?:VIOLATED|MET)\)} $t -> sl]} {
        return $sl
    }
    if {[regexp -- {(-?[0-9]+(?:\.[0-9]+)?)\s+slack} $t -> sl]} {
        return $sl
    }
    return "NA"
}
set ws_clk_core [group_setup_ws clk_core]
set ws_clk_apb  [group_setup_ws clk_apb]
set ws_async    [group_setup_ws asynchronous]

if {$setup_ws ne "NA" && [expr {$setup_ws < 0}] && ($setup_v eq "0" || $setup_v eq "NA")} {
    puts stderr "sta.tcl: ERROR: setup_ws=$setup_ws but setup_violations=$setup_v"
    set status "fail"
}

if {![info exists status]} {
    set status "ok"
    if {$nclk == 0} {
        set status "fail"
    } elseif {$setup_ws eq "NA" && $hold_ws eq "NA" && $wns eq "NA"} {
        set status "fail"
    } elseif {$setup_ws eq "NA" || $hold_ws eq "NA"} {
        set status "partial"
    }
}

set fmt full_clock_expanded
set path_fields {slew cap fanout input_pin net}

# --- existing core reports (enriched) ---------------------------------
write_rpt check_setup.rpt {
    check_setup -verbose
}

set metrics [open [file join $OUT metrics.json] w]
puts $metrics "\{"
puts $metrics "  \"stage\": [json_quote $stage],"
puts $metrics "  \"odb\": [json_quote [file normalize $ODB]],"
puts $metrics "  \"sdc\": [json_quote [file normalize $SDC]],"
puts $metrics "  \"liberty\": [json_quote [file normalize $LIBERTY]],"
puts $metrics "  \"corner\": [json_quote $CORNER],"
puts $metrics "  \"parasitics\": \"$par\","
puts $metrics "  \"propagated_clock\": [expr {$prop ? "true" : "false"}],"
puts $metrics "  \"clock_count\": $nclk,"
puts $metrics "  \"status\": \"$status\","
puts $metrics "  \"setup_ws_ns\": \"$setup_ws\","
puts $metrics "  \"setup_tns_ns\": \"$setup_tns\","
puts $metrics "  \"hold_ws_ns\": \"$hold_ws\","
puts $metrics "  \"hold_tns_ns\": \"$hold_tns\","
puts $metrics "  \"wns_ns\": \"$wns\","
puts $metrics "  \"ws_clk_core_ns\": \"$ws_clk_core\","
puts $metrics "  \"ws_clk_apb_ns\": \"$ws_clk_apb\","
puts $metrics "  \"ws_async_ns\": \"$ws_async\","
puts $metrics "  \"clk_period_ns\": \"$clk_period\","
puts $metrics "  \"period_min_ns\": \"$period_min\","
puts $metrics "  \"fmax_mhz\": \"$fmax_reg2reg\","
puts $metrics "  \"fmax_reg2reg_mhz\": \"$fmax_reg2reg\","
puts $metrics "  \"fmax_source\": \"report_clock_min_period (reg2reg; excludes IO)\","
puts $metrics "  \"setup_violations\": \"$setup_v\","
puts $metrics "  \"hold_violations\": \"$hold_v\","
puts $metrics "  \"max_slew_violations\": \"$slew_v\","
puts $metrics "  \"max_cap_violations\": \"$cap_v\","
puts $metrics "  \"max_fanout_violations\": \"$fanout_v\""
puts $metrics "\}"
close $metrics

write_rpt summary.rpt {
    puts "# corner $::CORNER | parasitics $::par | clocks $::nclk | status $::status"
    puts "# fmax_reg2reg=$::fmax_reg2reg MHz from report_clock_min_period (not WNS-derived)"
    puts "# setup_violations=$::setup_v hold_violations=$::hold_v"
    puts "# path-group WNS (setup): clk_core=$::ws_clk_core  clk_apb=$::ws_clk_apb  asynchronous=$::ws_async"
    puts "# blended setup_ws includes async recovery — use path-group columns for handoff"
    puts ""
    puts "=== setup ==="
    report_worst_slack -max -digits 4
    report_tns -max -digits 4
    report_wns -max -digits 4
    puts ""
    puts "=== hold ==="
    report_worst_slack -min -digits 4
    report_tns -min -digits 4
    report_wns -min -digits 4
    puts ""
    puts "=== aggregate ==="
    report_wns -digits 4
    report_tns -digits 4
    puts ""
    catch {report_clock_min_period}
}

write_rpt paths_setup.rpt {
    report_checks -path_delay max \
        -format $::fmt \
        -fields $::path_fields \
        -group_path_count $::PATHS \
        -digits 3
}

write_rpt paths_hold.rpt {
    report_checks -path_delay min \
        -format $::fmt \
        -fields $::path_fields \
        -group_path_count $::PATHS \
        -digits 3
}

# --- additional integrated reports ------------------------------------
write_rpt max_slew.rpt {
    report_check_types -max_slew -verbose
}

write_rpt max_cap.rpt {
    report_check_types -max_capacitance -verbose
}

write_rpt max_fanout.rpt {
    report_check_types -max_fanout -verbose
}

write_rpt unconstrained.rpt {
    puts "# Unconstrained endpoints (check_setup -unconstrained_endpoints -verbose)"
    puts "# Empty body means every endpoint is covered by timing constraints."
    puts ""
    check_setup -unconstrained_endpoints -verbose
}

write_rpt clock_summary.rpt {
    report_clock_properties
    puts ""
    if {$::prop} {
        puts "--- skew (setup) ---"
        catch {report_clock_skew -setup}
        puts ""
        puts "--- skew (hold) ---"
        catch {report_clock_skew -hold}
        puts ""
        puts "--- latency ---"
        catch {report_clock_latency}
    } else {
        puts "ideal clocks: skew/latency not meaningful before CTS"
    }
    puts ""
    catch {report_clock_min_period}
}

write_rpt recovery_removal.rpt {
    puts "=== recovery ==="
    report_check_types -recovery -verbose
    puts ""
    puts "=== removal ==="
    report_check_types -removal -verbose
}

write_rpt path_groups.rpt {
    set groups {}
    catch { set groups [sta::path_group_names] }
    if {[llength $groups] == 0} {
        puts "(no path groups)"
    }
    foreach g $groups {
        puts "=== path group: $g (setup) ==="
        catch {
            report_checks -path_delay max -path_group $g \
                -group_path_count 5 -format end -digits 3
        }
        puts ""
        puts "=== path group: $g (hold) ==="
        catch {
            report_checks -path_delay min -path_group $g \
                -group_path_count 5 -format end -digits 3
        }
        puts ""
    }
}

write_rpt design_summary.rpt {
    catch {report_design_area}
    puts ""
    catch {report_cell_usage}
}

# Explicit IO/reg path-group reports (reg2reg / in2reg / reg2out).
ensure_io_path_groups
write_path_group_rpt reg2reg.rpt reg2reg $PATHS
write_path_group_rpt in2reg.rpt  in2reg  $PATHS
write_path_group_rpt reg2out.rpt reg2out $PATHS

write_critical_path_classification critical_paths.rpt $PATHS

puts "sta_corner ok corner=$CORNER status=$status out=$OUT setup_ws=$setup_ws hold_ws=$hold_ws"
if {$status eq "fail"} { exit 2 }
exit 0

} else {
    die "STA_MODE must be corner|shell (got '$MODE')"
}
