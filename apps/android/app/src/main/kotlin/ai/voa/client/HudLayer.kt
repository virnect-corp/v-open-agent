package ai.voa.client

import ai.voa.client.output.RenderSpec
import ai.voa.client.output.StatusKey
import ai.voa.client.output.StatusRow
import ai.voa.client.protocol.FlashMode
import android.content.Context
import ai.voa.client.protocol.Detection
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.TextView

/**
 * The glasses display area drawn over the camera view (SPEC 7.4): a popup for alerts and
 * notices, a full-screen or border flash, and the status bar. No detection boxes.
 * A 5% safe margin keeps text away from the edges.
 */
class HudLayer(context: Context) : FrameLayout(context) {
    private val main = Handler(Looper.getMainLooper())
    private val density = resources.displayMetrics.density
    private val flash = View(context).apply { visibility = INVISIBLE }
    private val safe = FrameLayout(context)
    private val popup = TextView(context).apply {
        visibility = GONE
        ellipsize = TextUtils.TruncateAt.END
        gravity = Gravity.CENTER
        val pad = (12 * density).toInt()
        setPadding(pad * 2, pad, pad * 2, pad)
    }
    private val status = TextView(context).apply {
        setTextColor(Color.WHITE)
        textSize = 18f
        setTypeface(typeface, Typeface.BOLD)
        val pad = (8 * density).toInt()
        setPadding(pad * 2, pad, pad * 2, pad)
        background = GradientDrawable().apply {
            cornerRadius = 999f
            setColor(0xCC000000.toInt())
            setStroke((1 * density).toInt(), 0x66FFFFFF)
        }
    }
    private val snapshot = ImageView(context).apply {
        visibility = GONE
        scaleType = ImageView.ScaleType.FIT_XY
        val pad = (3 * density).toInt()
        setPadding(pad, pad, pad, pad)
        setBackgroundColor(0xFFFF3B30.toInt())
    }
    private var hideSnapshot: Runnable? = null
    private var hideAt: Runnable? = null
    private val flashSteps = mutableListOf<Runnable>()

    init {
        addView(flash, LayoutParams(MATCH_PARENT, MATCH_PARENT))
        addView(safe, LayoutParams(MATCH_PARENT, MATCH_PARENT))
        safe.addView(snapshot, LayoutParams(MATCH_PARENT, MATCH_PARENT, Gravity.CENTER))
        safe.addView(status, LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.END))
        safe.addView(popup, LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.CENTER_HORIZONTAL))
        addOnLayoutChangeListener { _, l, t, r, b, _, _, _, _ ->
            val x = ((r - l) * 0.05f).toInt()
            val y = ((b - t) * 0.05f).toInt()
            safe.setPadding(x, y, x, y)
            // the judged scene: half the display width at 16:9 (the frame's shape), a little
            // below the middle so the top popup does not cover it
            val width = ((r - l) * 0.5f).toInt()
            snapshot.layoutParams = (snapshot.layoutParams as LayoutParams).apply {
                this.width = width
                height = width * 9 / 16
                gravity = Gravity.CENTER
            }
            snapshot.translationY = (b - t) * 0.08f
        }
    }

    /** Show a reduced presentation now; returns at display start (`played` time). */
    fun show(spec: RenderSpec) {
        hideAt?.let { main.removeCallbacks(it) }
        popup.text = spec.text
        popup.maxLines = spec.maxLines
        popup.textSize = if (spec.large) 26f else 20f
        popup.setTypeface(Typeface.DEFAULT, if (spec.large) Typeface.BOLD else Typeface.NORMAL)
        val color = spec.color?.let { colorOf(it) }
        popup.setTextColor(color ?: Color.BLACK)
        popup.background = GradientDrawable().apply {
            cornerRadius = 12 * density
            setColor(if (color == null) Color.WHITE else 0x99000000.toInt()) // mono: inverse emphasis
        }
        popup.layoutParams = (popup.layoutParams as LayoutParams).apply {
            gravity = when (spec.position) {
                "center" -> Gravity.CENTER
                "bottom_center" -> Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
                else -> Gravity.TOP or Gravity.CENTER_HORIZONTAL
            }
            topMargin = if (spec.position == "top_center") (40 * density).toInt() else 0 // below the status bar
        }
        popup.visibility = VISIBLE
        startFlash(spec, color ?: Color.WHITE)
        hideAt = Runnable { popup.visibility = GONE }.also { main.postDelayed(it, spec.showMs.toLong()) }
    }

    /**
     * Optional judged-scene view (AND-04A): the frame the server judged, with its boxes, in the
     * centre for `showMs`. Mission labels are solid red, others faint dashed (like the sim page).
     */
    fun showSnapshot(frame: Bitmap, detections: List<Detection>, missionLabels: Set<String>, showMs: Int) {
        val canvasBitmap = frame.copy(Bitmap.Config.ARGB_8888, true)
        val canvas = Canvas(canvasBitmap)
        val judged = Paint().apply {
            style = Paint.Style.STROKE
            strokeWidth = 4f
            color = 0xFFFF3B30.toInt()
        }
        val other = Paint().apply {
            style = Paint.Style.STROKE
            strokeWidth = 2f
            color = 0x99C8CCD2.toInt()
            pathEffect = DashPathEffect(floatArrayOf(8f, 8f), 0f)
        }
        val label = Paint().apply {
            textSize = 18f
            isAntiAlias = true
        }
        val w = canvasBitmap.width.toFloat()
        val h = canvasBitmap.height.toFloat()
        for (d in detections.sortedBy { it.label in missionLabels }) {
            val paint = if (d.label in missionLabels) judged else other
            val (x1, y1, x2, y2) = d.bbox
            canvas.drawRect(x1.toFloat() * w, y1.toFloat() * h, x2.toFloat() * w, y2.toFloat() * h, paint)
            label.color = paint.color
            canvas.drawText("${d.label} ${"%.2f".format(d.score)}", x1.toFloat() * w + 4, maxOf(18f, y1.toFloat() * h - 4), label)
        }
        hideSnapshot?.let { main.removeCallbacks(it) }
        snapshot.setImageBitmap(canvasBitmap)
        snapshot.visibility = VISIBLE
        hideSnapshot = Runnable { snapshot.visibility = GONE }.also { main.postDelayed(it, showMs.toLong()) }
    }

    fun hide() {
        hideAt?.let { main.removeCallbacks(it) }
        popup.visibility = GONE
        stopFlash()
    }

    fun setStatus(row: StatusRow) {
        val dot = when (row.key.tone) {
            "green-dot" -> "● "
            "yellow-dot", "yellow" -> "● "
            "red" -> "● "
            else -> "○ "
        }
        status.text = dot + row.key.label
        status.setTextColor(
            when (row.key) {
                StatusKey.NORMAL -> 0xFF34C759.toInt()
                StatusKey.CANDIDATE, StatusKey.SLOW -> 0xFFFFCC00.toInt()
                StatusKey.WARNING, StatusKey.SERVER_ERROR -> 0xFFFF6B61.toInt()
                else -> 0xFFC8CCD2.toInt()
            },
        )
    }

    private fun startFlash(spec: RenderSpec, color: Int) {
        stopFlash()
        if (spec.flashMode == FlashMode.NONE || spec.flashCount <= 0) return
        flash.background = if (spec.flashMode == FlashMode.FULL_SCREEN) {
            GradientDrawable().apply { setColor((color and 0x00FFFFFF) or 0x73000000) }
        } else {
            GradientDrawable().apply { setStroke((8 * density).toInt(), color) }
        }
        val period = (spec.onMs + spec.offMs).toLong()
        repeat(spec.flashCount) { i ->
            val on = Runnable { flash.visibility = VISIBLE }
            val off = Runnable { flash.visibility = INVISIBLE }
            flashSteps += on
            flashSteps += off
            main.postDelayed(on, i * period)
            main.postDelayed(off, i * period + spec.onMs)
        }
    }

    private fun stopFlash() {
        flashSteps.forEach { main.removeCallbacks(it) }
        flashSteps.clear()
        flash.visibility = INVISIBLE
    }

    private fun colorOf(name: String): Int = when (name) {
        "red" -> 0xFFFF3B30.toInt()
        "yellow" -> 0xFFFFCC00.toInt()
        else -> Color.WHITE
    }
}
