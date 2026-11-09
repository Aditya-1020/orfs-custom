# Extensions

ASICFlow supports Python CLI plugins and explicitly configured Tcl extensions. It does not scan directories or execute arbitrary files automatically.

## Python command plugins

Declare an entry point in the asicflow.plugins group:

    [project.entry-points."asicflow.plugins"]
    macro_report = "my_asicflow_plugin:register_cli"

The target must expose a callable with this interface:

    def register_cli(subparsers, add_common_arguments):
        parser = subparsers.add_parser(
            "macro-report",
            help="Report macro pin accessibility",
        )
        add_common_arguments(parser)
        parser.set_defaults(func=run_macro_report)

    def run_macro_report(args):
        from asicflow.config import ctx_from_args
        ctx = ctx_from_args(args)
        print(ctx.results_dir)
        return 0

Plugins register their own parser and handler. They should not patch ASICFlow modules or mutate global configuration.

## Tcl extensions

List Tcl files explicitly in flow.yaml, a design profile, or ASICFLOW_TCL_EXTENSIONS:

    extensions:
      tcl:
        - tcl/custom/check_macro_access.tcl
        - tcl/custom/report_power_grid.tcl

Environment paths are separated using the host platform path separator. Files are validated and written into a generated loader. The loader is sourced by standalone STA after the design, constraints and RC setup have been loaded.

For other ORFS stages, connect the generated loader to the appropriate ORFS pre/post-stage hook in the design Makefile. ASICFlow deliberately does not guess which hook a custom ORFS fork implements.

Tcl extensions execute inside OpenROAD. Treat them as trusted code and keep them under version control.
