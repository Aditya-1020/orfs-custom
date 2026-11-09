# sta_pre.tcl -- pre-layout timing for any module (replaces sta_gen / sta_postplace / sta_shell)
# Yosys synthesis (liberty techmap) -> OpenSTA analysis, for any RTL.
#
# Usage (run from the repo root, Makefile fills in every variable below):
#   make presta TECH=sky130|nangate45        synth + STA, reports in <WORK_HOME>/reports/<platform>/pre_sta/
#   make presta-shell TECH=...               reuse the netlist (SKIP_SYNTH=1) and stay in the shell
#   by hand: RTL_FILES="a.sv b.sv" LIBERTY=<lib> openroad -no_splash -exit asicflow/tcl/sta_pre.tcl
#
# Configuration is read from environment variables. Empty means default.
#   RTL_FILES      space-separated files or globs (RTL_FILES or RTL_FILELIST required)
#   RTL_FILELIST   text file with one path per line; blank lines, # and // comments ignored
#   RTL_ROOT       prefix for relative paths in RTL_FILELIST (e.g. rtl/)
#   MACRO_LIBS     macro liberty files (space separated); a sibling <name>.bb.v is read as the blackbox
#   TOP            top module; auto-detected when empty
#   LIBERTY        liberty file (default: $PLATFORM_LIB)
#   SDC_FILE       constraints file; when empty, internal default constraints are used
#   INCLUDE_DIRS   space-separated include directories
#   DEFINES        space-separated NAME or NAME=VALUE
#   PARAMS         space-separated NAME=VALUE top-level parameter overrides (requires TOP)
#   USE_SLANG      1 to read with the yosys-slang plugin (default 0)
#   DONT_USE       space-separated cell globs, or "none" (default *lpflow*)
#   OUT_DIR        output directory (default ./sta_out)
#   SKIP_SYNTH     1 to reuse $OUT_DIR/netlist.v instead of running Yosys
#   PATH_COUNT     endpoints listed in the setup endpoint report (default 10)
# Used only when SDC_FILE is empty:
#   CLK_PORT       clock port; auto-detected from port names when empty,
#                  virtual clock if none is found
#   CLK_PERIOD     ns (default 3.333)
#   CLK_UNCERT     ns (default 0.10)
#   IO_DELAY_PCT   input/output delay as a fraction of the period (default 0.20)
#   IN_TRANS       input slew, ns (default 0.10)
#   OUT_LOAD       output load, liberty cap unit (default 0.02)
# Optional extra report:
#   HS_FROM, HS_TO ports for an explicit port-to-port path report

proc cfg {name default} {
    if {[info exists ::env($name)] && [string trim $::env($name)] ne ""} {
        return [string trim $::env($name)]
    }
    return $default
}

proc expand_files {patterns} {
    set out {}
    foreach p $patterns {
        if {[regexp {[*?\[]} $p]} {
            set m [lsort [glob -nocomplain $p]]
            if {[llength $m] == 0} { error "No files match: $p" }
            lappend out {*}$m
        } else {
            lappend out $p
        }
    }
    return $out
}

proc read_filelist {path} {
    set fh [open $path r]
    set lines [split [read $fh] \n]
    close $fh
    set out {}
    foreach l $lines {
        set l [string trim $l]
        if {$l eq "" || [string match {#*} $l] || [string match {//*} $l]} continue
        lappend out $l
    }
    return $out
}

set TOP          [cfg TOP ""]
set RTL_FILES    [cfg RTL_FILES ""]
set RTL_FILELIST [cfg RTL_FILELIST ""]
set RTL_ROOT     [cfg RTL_ROOT ""]
set MACRO_LIBS   [cfg MACRO_LIBS ""]
set INCLUDE_DIRS [cfg INCLUDE_DIRS ""]
set DEFINES      [cfg DEFINES ""]
set PARAMS       [cfg PARAMS ""]
set LIBERTY      [cfg LIBERTY [cfg PLATFORM_LIB ""]]
set SDC_FILE     [cfg SDC_FILE ""]
set USE_SLANG    [cfg USE_SLANG 0]
set DONT_USE     [cfg DONT_USE {*lpflow*}]
set OUT_DIR      [cfg OUT_DIR ./sta_out]
set SKIP_SYNTH   [cfg SKIP_SYNTH 0]
set PATH_COUNT   [cfg PATH_COUNT 10]

set CLK_PORT     [cfg CLK_PORT ""]
set CLK_PERIOD   [cfg CLK_PERIOD 3.333]
set CLK_UNCERT   [cfg CLK_UNCERT 0.10]
set IO_DELAY_PCT [cfg IO_DELAY_PCT 0.20]
set IN_TRANS     [cfg IN_TRANS 0.10]
set OUT_LOAD     [cfg OUT_LOAD 0.02]

set HS_FROM      [cfg HS_FROM ""]
set HS_TO        [cfg HS_TO ""]

if {$DONT_USE eq "none"} { set DONT_USE {} }

if {[auto_execok yosys] eq ""} { error "yosys not found in PATH" }

set rtl [expand_files $RTL_FILES]
if {$RTL_FILELIST ne ""} {
    if {![file exists $RTL_FILELIST]} { error "RTL_FILELIST not found: $RTL_FILELIST" }
    foreach f [read_filelist $RTL_FILELIST] {
        if {$RTL_ROOT ne "" && [file pathtype $f] eq "relative"} { set f [file join $RTL_ROOT $f] }
        lappend rtl {*}[expand_files [list $f]]
    }
}
foreach m $MACRO_LIBS {
    set bb "[file rootname $m].bb.v"
    if {[file exists $bb]} { lappend rtl $bb }
}
# same file listed twice (filelist + RTL_FILES) would be a duplicate module
set uniq {}
foreach f $rtl {
    set n [file normalize $f]
    if {[lsearch -exact $uniq $n] < 0} { lappend uniq $n }
}
set rtl $uniq
if {[llength $rtl] == 0} { error "No RTL given. Set RTL_FILES or RTL_FILELIST" }
if {$LIBERTY eq ""} { error "No liberty given. Set LIBERTY or PLATFORM_LIB" }

set required [concat $rtl [list $LIBERTY] $MACRO_LIBS]
if {$SDC_FILE ne ""} { lappend required $SDC_FILE }
foreach f $required {
    if {![file exists $f]} { error "File not found: $f" }
}

if {$PARAMS ne "" && $TOP eq ""} { error "PARAMS requires TOP" }

file mkdir $OUT_DIR
set period_ps [expr {int(round($CLK_PERIOD * 1000))}]

set inc_flags ""
foreach d $INCLUDE_DIRS { append inc_flags " -I $d" }
set def_flags ""
foreach d $DEFINES { append def_flags " -D $d" }

set chparams ""
set slang_params ""
foreach kv $PARAMS {
    if {![regexp {^([^=]+)=(.*)$} $kv -> k v]} {
        error "Bad PARAMS entry '$kv', expected NAME=VALUE"
    }
    append chparams " -chparam $k $v"
    append slang_params " -G $kv"
}

set dfl_cmd "dfflibmap -liberty $LIBERTY"
set abc_cmd "abc -liberty $LIBERTY -D $period_ps"
foreach p $DONT_USE {
    append dfl_cmd " -dont_use $p"
    append abc_cmd " -dont_use $p"
}

set ys [open "$OUT_DIR/synth.ys" w]
puts $ys "read_liberty -overwrite -setattr liberty_cell -lib $LIBERTY"
if {$USE_SLANG} {
    set top_flag ""
    if {$TOP ne ""} { set top_flag " --top $TOP" }
    puts $ys "plugin -i slang"
    puts $ys "read_slang$top_flag$inc_flags$def_flags$slang_params [join $rtl { }]"
    set chparams ""
} else {
    puts $ys "read_verilog -sv$inc_flags$def_flags [join $rtl { }]"
}
if {$TOP ne ""} {
    puts $ys "hierarchy -check -top $TOP$chparams"
} else {
    puts $ys "hierarchy -check -auto-top"
}
puts $ys "synth -flatten"
puts $ys "opt_clean -purge"
puts $ys $dfl_cmd
puts $ys "opt"
puts $ys $abc_cmd
puts $ys "opt_clean -purge"
puts $ys "rename -unescape"
set stat_libs "-liberty $LIBERTY"
foreach m $MACRO_LIBS { append stat_libs " -liberty $m" }
puts $ys "tee -o $OUT_DIR/area.rpt stat $stat_libs"
puts $ys "write_verilog -noattr -noexpr -nohex -nodec $OUT_DIR/netlist.v"
close $ys

puts "Running Yosys synthesis..."
if {$SKIP_SYNTH && [file exists "$OUT_DIR/netlist.v"]} {
    puts "SKIP_SYNTH: reusing $OUT_DIR/netlist.v"
} elseif {[catch {
    exec yosys -l "$OUT_DIR/yosys.log" "$OUT_DIR/synth.ys" > /dev/null 2>@1
} err]} {
    puts stderr "Yosys failed: $err"
    puts stderr "Inspect $OUT_DIR/yosys.log"
    exit 1
}

puts "\n=== Yosys area ==="
if {[file exists "$OUT_DIR/area.rpt"]} {
    set fh [open "$OUT_DIR/area.rpt" r]
    set area_text [read $fh]
    close $fh
    foreach l [split $area_text \n] {
        if {[regexp {Chip area|sequential elements| cells$} $l]} { puts $l }
    }
}

set netlist_file "$OUT_DIR/netlist.v"
set fh [open $netlist_file r]
set netlist [read $fh]
close $fh

# OpenSTA's verilog reader rejects the `signed` keyword
set count [regsub -all {\msigned } $netlist {} netlist]
set fh [open $netlist_file w]
puts -nonewline $fh $netlist
close $fh
puts "Stripped $count 'signed' keywords from $netlist_file"

if {$TOP eq ""} {
    if {![regexp {(?:^|\n)module\s+([A-Za-z_][A-Za-z0-9_$]*)} $netlist -> TOP]} {
        error "Could not determine top module from $netlist_file"
    }
    puts "Detected top module: $TOP"
}

read_liberty $LIBERTY
foreach m $MACRO_LIBS { read_liberty $m }
read_verilog $netlist_file
link_design $TOP

set clk_names {}
if {$CLK_PORT ne ""} {
    set clk_names [list $CLK_PORT]
} else {
    set decl {input\s+(?:\[[^\]]*\]\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*;}
    foreach {whole name} [regexp -all -inline $decl $netlist] {
        if {[regexp -nocase {clk|clock} $name]} { lappend clk_names $name }
    }
}

set clk_objs {}
foreach n $clk_names {
    set o [get_ports $n]
    if {[llength $o] == 0} { error "Clock port '$n' not found in $TOP" }
    lappend clk_objs {*}$o
}

set data_excl $clk_objs

if {$SDC_FILE ne ""} {
    puts "Constraints: $SDC_FILE"
    read_sdc $SDC_FILE
} else {
    puts "Constraints: internal defaults"
    if {[llength $clk_objs] > 0} {
        if {[llength $clk_objs] > 1 && $CLK_PORT eq ""} {
            puts "Warning: multiple clock-like ports ([join $clk_names {, }]); using [lindex $clk_names 0]. Set CLK_PORT or SDC_FILE."
        }
        set main_clk [lindex $clk_objs 0]
        create_clock -name clk -period $CLK_PERIOD $main_clk
        set data_excl [list $main_clk]
    } else {
        puts "Warning: no clock port found, using a virtual clock"
        create_clock -name clk -period $CLK_PERIOD
        set data_excl {}
    }
    set_clock_uncertainty $CLK_UNCERT [get_clocks clk]

    set io_delay [expr {$IO_DELAY_PCT * $CLK_PERIOD}]
    set d_in [all_inputs]
    foreach c $data_excl { set d_in [lsearch -inline -all -not -exact $d_in $c] }
    if {[llength $d_in] > 0} {
        set_input_transition $IN_TRANS $d_in
        set_input_delay -clock clk $io_delay $d_in
    }
    if {[llength [all_outputs]] > 0} {
        set_output_delay -clock clk $io_delay [all_outputs]
        set_load $OUT_LOAD [all_outputs]
    }
}

set data_in [all_inputs]
foreach c $data_excl { set data_in [lsearch -inline -all -not -exact $data_in $c] }

puts "\n=== Check setup ==="
check_setup -verbose

report_checks -path_delay max -group_path_count $PATH_COUNT -format end \
    > "$OUT_DIR/setup_endpoints.rpt"
report_checks -path_delay max -fields {nets fanout cap slew} -digits 3 \
    > "$OUT_DIR/setup_worst.rpt"
report_checks -path_delay min -fields {nets fanout cap slew} -digits 3 \
    > "$OUT_DIR/hold_worst.rpt"

set regs_ck [all_registers -clock_pins]
set regs_d  [all_registers -data_pins]
set has_regs [expr {[llength $regs_ck] > 0}]

if {$has_regs} {
    report_checks -path_delay max -from $regs_ck -to $regs_d -digits 3 \
        > "$OUT_DIR/reg2reg.rpt"
    if {[llength $data_in] > 0} {
        report_checks -path_delay max -from $data_in -to $regs_d -digits 3 \
            > "$OUT_DIR/in2reg.rpt"
    }
    if {[llength [all_outputs]] > 0} {
        report_checks -path_delay max -from $regs_ck -to [all_outputs] -digits 3 \
            > "$OUT_DIR/reg2out.rpt"
    }
}

if {$HS_FROM ne "" && $HS_TO ne ""} {
    report_checks -path_delay max -from [get_ports $HS_FROM] -to [get_ports $HS_TO] \
        -digits 3 > "$OUT_DIR/port2port.rpt"
}

report_check_types -max_slew -max_capacitance -violators \
    > "$OUT_DIR/violations.rpt"

puts "\n=== Timing summary ==="
report_checks -path_delay max -format summary
report_checks -path_delay min -format summary
report_wns
report_tns
report_worst_slack -max
report_worst_slack -min

if {$has_regs} {
    puts "\n--- Register-to-register minimum period ---"
    report_clock_min_period
    puts "\n--- Including port paths ---"
    report_clock_min_period -include_port_paths
}

puts "\nReports written to $OUT_DIR/"