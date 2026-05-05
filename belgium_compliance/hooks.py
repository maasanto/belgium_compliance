app_name = "belgium_compliance"
app_title = "Belgium Compliance"
app_publisher = "Antoine Maas"
app_description = (
	"Belgian regional compliance for ERPNext (PCMN, VAT declaration, INTERVAT, intracommunity reporting)"
)
app_email = "antoine.maas@gmail.com"
app_license = "gpl-3.0"

# Required apps
# ------------------
required_apps = ["erpnext"]

# Installation
# ------------
after_install = "belgium_compliance.install.after_install"

# Fixtures
# --------
fixtures = [
	{"dt": "Belgian VAT Grid"},
	{"dt": "Belgian VAT Tax Definition"},
]
